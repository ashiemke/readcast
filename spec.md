# readcast — implementation spec

A self-hosted service. It turns a web article into a narrated episode in a private
podcast feed. One click in the browser starts the work.

This document is the build brief. Follow it in the milestone order in section 17.

Revised after living with the built system. Changes from the first draft are
marked **[revised]** with the reason, because the reason is usually the
interesting part.

---

## 1. Purpose

Save an article from the browser. Get a listenable episode in any podcast app a
few minutes later. The audio must survive a 40-minute listen without annoying
the listener.

Two properties matter more than anything else:

1. **The text that reaches the speech engine is a durable, editable file.** The
   operator can read it, find a bug, fix a rule, and re-render one episode.
2. **Pronunciation and cleanup rules live in version control.** The operator
   tunes them over months. `git diff` is the review tool.

Everything else in this spec supports those two properties.

## 2. Non-goals

Do not build these. Do not add them later without a request.

- No summarization. No LLM rewrite. No two-host "AI podcast" format. The
  listener wants the article read, in full, in order.
- No multi-user support. No login. No user table.
- **[revised]** A web dashboard is now in scope. The read-only status page was
  not enough once articles queued up for hours: the operator needs to see what
  is running, reorder what runs next, and read the prepared text before paying
  for the audio. It stays a single page, single user, token-gated.
- No publishing to podcast directories.
- No mobile app.
- No cloud dependency in the default path.

## 3. Architecture

```
chrome extension ──HTTPS POST──┐
bookmarklet / curl / CLI ──────┤
                               ▼
                       FastAPI  /jobs
                               │
                               ▼
                    SQLite job queue
                               │
        ┌──────────────────────┴───────────────────────┐
        ▼  TEXT LANE (fast, immediate)                 │
 fetch → extract → prepare ──► state: ready ───────────┤
   │        │         │        spoken.txt on disk,     │
raw.html extracted.md spoken.txt   editable            │
                   transforms.jsonl                    ▼
                   unknowns.jsonl        AUDIO LANE (slow, one at a time)
                                          chunk → synth → assemble → publish
                                                    │        │          │
                                             chunks/*.wav episode.mp3 feed.xml
```

Each stage writes its output to disk under the job directory. Each stage reads
only the previous stage's file. This makes every stage independently
re-runnable.

**[revised] Two lanes, not one.** The original had a single worker for the whole
pipeline. In practice a 6,000-word article is a two-hour render, so a job
submitted behind one waited hours before its text existed — and the text is the
thing the operator wants to see and fix. Preparation is seconds of CPU and
touches no model, so it runs immediately in its own lane with its own lock. The
one-at-a-time rule was always about the speech model's memory; it now applies
only to the lane that loads it.

A job that finishes preparation stops in `ready`. `spoken.txt` is on disk and
can be read, hand-edited, or re-prepared before anything expensive starts.

## 4. Stack

| Concern | Choice | Note |
| --- | --- | --- |
| Language | Python 3.12 | |
| Packaging | `uv` | Lock the dependencies. |
| API | FastAPI + uvicorn | |
| State | SQLite | One file. WAL mode. |
| Fetch | `httpx` | |
| Fetch fallback | Playwright (Chromium) | Only for pages that fail extraction. |
| Extract | `trafilatura` | Output markdown. Keep headings and paragraph breaks. |
| Numbers to words | `num2words` | |
| Speech | `mlx-audio` HTTP server | OpenAI-compatible `/v1/audio/speech`. |
| Speech check | `mlx-whisper` | Detects dropped words. See section 9.4. |
| Audio | `ffmpeg` | Concatenate, pad, loudness, encode. |
| Tags | `mutagen` | ID3 including chapters. |
| Feed | `feedgen` | RSS 2.0 plus the iTunes namespace. |

Install `mlx-audio` at version 0.5.1 or later. That release added Breeze TTS 2.
Run it as a separate process. Do not import the model into the API process.

## 5. Directory layout

```
readcast/
  config.yml
  rules/
    strip.yml
    normalize.yml
    lexicon.yml
    tests.yml
    domains/
      arstechnica.com.yml
      example.substack.com.yml
  data/
    readcast.db
    jobs/
      2026-09-09-a1b2c3/
        job.json
        raw.html
        extracted.md
        spoken.txt
        transforms.jsonl
        unknowns.jsonl
        chunks/000.wav ...
        episode.mp3
    feed/
      feed.xml
      cover.jpg
  src/readcast/
    api.py worker.py fetch.py extract.py prepare/ chunk.py synth/ assemble.py
    feed.py cli.py dashboard.py audio.py db.py config.py
    web/dashboard.html          # the browser dashboard, one self-contained file
    data/words_en.txt.gz        # bundled wordlist for unknown-term discovery
  extension/                    # the Chrome extension (section 12.3)
  deploy/                       # launchd agents
```

Inside a job directory, `chunks/` holds `NNN.wav`, a `NNN.txt` sidecar with the
exact text that produced it, and a `.cache/<sha1>.wav` of the same audio keyed by
text. See section 10.7.

Keep `rules/` a git repository. Keep `data/` out of git.

## 6. Job model

Table `jobs`:

`id` (text, `YYYY-MM-DD-<6 char base32>`), `url`, `title`, `author`,
`publication`, `published_at`, `submitted_at`, `state`, `stage_failed`,
`error`, `backend`, `model`, `voice`, `duration_s`, `bytes`, `rtf`, `client_html`
(boolean), `flagged_chunks` (integer), `word_count`, `render_version`, `in_feed`,
`pending_from`, `hold` (boolean), `queue_rank`.

States: `queued`, `fetching`, `extracting`, `preparing`, **`ready`**,
`synthesizing`, `assembling`, `done`, `failed`.

`ready` is the pause between the lanes: the text exists, nothing expensive has
happened, and the operator may edit it, reorder it, or hold it.

Rules:

1. Run exactly one **audio** worker. The speech model holds several gigabytes of
   memory. A second worker doubles that and slows both jobs. The text lane runs
   its own single worker alongside, with a separate lock.
   **[revised]** A worker that cannot take its lock must keep trying rather than
   give up. A service restart races its own predecessor, and a server that
   quietly runs nothing looks identical to a healthy one.
2. On failure, keep every artifact already written. Set `state = failed` and
   record `stage_failed` and `error`.
3. A failed job never enters the feed.
4. Record the wall time of every stage. Record the real-time factor
   (`audio_seconds / synthesis_seconds`) on the job row. The operator needs this
   number to compare a local backend against a paid one.
5. **[revised]** `submitted_at` needs sub-second precision. At second precision,
   several submissions inside one second sorted by the random part of the job
   id, so the queue was not first in, first out.
6. **[revised]** Adding a column to a released schema requires a migration.
   `CREATE TABLE IF NOT EXISTS` does not alter an existing table, so every new
   column must be added explicitly on open.
7. **[revised]** A job left in a running state by a crash or a restart must be
   reaped when a worker takes the lock: mark it `failed` with the stage it died
   in, keep its artifacts, and tell the operator the command that resumes it.

## 7. Fetch

1. If the request body contains `client_html`, use it. Skip the network fetch.
2. Otherwise fetch the URL with `httpx`. Send a normal browser user agent. Follow
   redirects. Time out after 20 seconds.
3. If extraction (section 8) returns fewer than 500 characters, retry the fetch
   with Playwright. Wait for the network to go idle. Then extract again.
4. If the second attempt also returns fewer than 500 characters, fail the job
   with `error = "extraction produced too little text"`.

`client_html` matters. The browser already rendered the page and already holds
the operator's session. This is how a paywalled or JavaScript-heavy article gets
through. Cap the field at 4 MB. Reject a larger body with HTTP 413.

## 8. Extract

Use `trafilatura` with `output_format="markdown"`, `include_comments=False`,
`include_tables=False`, `favor_precision=True`.

**[revised]** Precision mode drops every heading on some sites — Wikipedia among
them — and headings drive chunking, pauses and chapter marks. When the precision
pass finds no heading at all and the recall pass does, use the recall pass. A
domain file may set `extract.favor_precision: false` outright.

Write `extracted.md`. Preserve these structures, because later stages need them:

- Heading level and heading text.
- Paragraph boundaries.
- Blockquote markers.
- Code fences.

Read metadata (title, author, publication, publish date) from `trafilatura`
first. Fall back to Open Graph tags. Fall back to the `<title>` element.

A per-domain file may override extraction. See section 10.5.

## 9. The text preparation stage

This is the part the operator tunes. Design it for change.

### 9.1 Order of operations

Run these steps once, in this order, in a single pass over the text:

1. **Structural strip** — remove whole blocks (code, tables, image alt text),
   unwrap links to their text, and strip inline markup. **[revised]** Three
   traps here, all found on real pages:
   - `trafilatura` leaves HTML fragments in its markdown (Wikipedia's `<sup>`
     citation markers). A tag read aloud is worse than no tag, so strip them.
   - A `<pre><code>` block can arrive as a single backticked line rather than a
     fence. Treat it as a code block.
   - Emphasis stripping must not cross a paragraph break and must be applied
     repeatedly. With an unbounded dot-matches-newline pattern, one stray
     asterisk pairs with another thousands of characters away; with a single
     pass, `**a *b* c**` leaves the inner pair behind, because a substitution is
     not rescanned. Delete any asterisk still standing afterwards.
2. **Pattern strip** — delete matched spans (figure references, citation
   markers).
3. **Pre-builtin rules** — operator regex rules that must run before the number
   handling.
4. **Builtins** — numbers, currency, dates, ranges, percent, units, symbols,
   URLs.
5. **Post-builtin rules** — operator regex rules that must run after the number
   handling.
6. **Lexicon** — per-term pronunciation and expansion.

### 9.2 The frozen-span rule

This rule prevents the worst class of bug in a rule pipeline.

When a rule replaces a span, mark the replacement span as frozen. No later rule
may match inside a frozen span.

Without this rule, one rule rewrites text and the next rule mangles the result.
An example: a currency rule turns `$1.2M` into `one point two million dollars`,
and then a units rule finds `M` somewhere and edits it again. The frozen mask
stops that.

Apply lexicon terms longest match first. `AIX` must not match the rule for `AI`.

### 9.3 Outputs

Write three files.

**`spoken.txt`** — exactly the text sent to the speech engine, with a blank line
at every paragraph break and a `## ` prefix retained at every heading. This file
is the operator's debugging surface. When the audio says something wrong, the
operator reads this file first.

**`transforms.jsonl`** — one JSON object per applied rule:

```json
{"rule":"currency-magnitude","file":"normalize.yml","offset":1841,"before":"$1.2M","after":"one point two million dollars"}
```

The operator greps this file to answer "which rule did that?".

**`unknowns.jsonl`** — candidate terms with no lexicon entry. See section 9.7.

### 9.4 rules/strip.yml

Everything here is deleted. Nothing here reaches the speech engine.

```yaml
version: 1

structural:
  code_blocks: drop          # drop | announce
  code_announce_text: "Code block omitted."
  tables: drop
  image_alt: drop
  blockquotes: keep
  headings: keep

patterns:
  - id: parenthetical-figure-ref
    match: '\((?:see\s+)?(?:Fig(?:ure)?|Tab(?:le)?|Eq(?:uation)?)\.?\s*\d+[a-z]?\)'
    flags: i
    note: "(Fig. 3)  (see Figure 2b)  (Table 1)"

  - id: bracket-citation
    match: '\[\d+(?:\s*[,\u2013-]\s*\d+)*\]'
    note: "[1]  [2,3]  [4-6]"

  - id: footnote-marker
    match: '[\u00b9\u00b2\u00b3\u2070-\u2079\u2020\u2021]{1,2}'
    note: "superscript digits, dagger, double dagger"

  - id: editorial-marker
    match: '\[(citation needed|sic|emphasis added|clarification needed)\]'
    flags: i

  - id: social-boilerplate
    match: '^(Share this|Follow us on|Sign up for our newsletter).*$'
    flags: im
```

### 9.5 rules/normalize.yml

Two kinds of entry live here. Builtins carry the load. Operator regex handles the
rest.

```yaml
version: 1

builtins:
  numbers:
    enabled: true
    max_digits_spelled: 9        # longer runs stay as digits
    year_style: pairs            # 1984 -> "nineteen eighty-four"
    ordinals: true               # 3rd -> "third"
  currency:
    enabled: true
    default_code: USD
    magnitudes: {K: thousand, M: million, B: billion, T: trillion}
  dates:
    enabled: true
    style: month_day_year        # 2026-08-25 -> "August twenty-fifth, twenty twenty-six"
  ranges:
    enabled: true
    joiner: " to "               # "3-5 days" -> "three to five days"
  percent:
    enabled: true                # "12%" -> "twelve percent"
  units:
    enabled: true
    table:
      km: kilometers
      kg: kilograms
      GB: gigabytes
      ms: milliseconds
      "°F": degrees Fahrenheit
  urls:
    enabled: true
    action: drop                 # drop | say_domain | read
    drop_replacement: ""
  symbols:
    enabled: true
    table:
      "~": approximately
      "≈": approximately
      "±": plus or minus
      "→": leads to
      "&": and
      "×": times
      "∴": therefore

rules:
  - id: inline-figure-ref
    phase: pre_builtin
    match: '\b(Fig|Tab|Eq)\.\s*(\d+)([a-z])?'
    say: '$1_EXPANDED $2 $3'
    expand: {Fig: Figure, Tab: Table, Eq: Equation}
    note: >
      Keeps a reference that is the subject of the sentence.
      strip.yml already removed the parenthetical form.

  - id: slash-ambiguity
    phase: post_builtin
    match: '(\w+)/(\w+)'
    say: '$1 or $2'
    note: >
      A slash means "or", "per", or "and" depending on context.
      Default to "or". Add a lexicon entry for any specific term that needs
      something else, for example "km/h".

  - id: abbreviation-latin
    phase: pre_builtin
    match: '\b(e\.g\.|i\.e\.|cf\.|viz\.)'
    say: MAP
    map:
      "e.g.": "for example"
      "i.e.": "that is"
      "cf.": "compare"
      "viz.": "namely"
```

Rule fields: `id`, `phase`, `match`, `flags`, `say`, `expand`, `map`, `enabled`
(default true), `note`, `scope` (see section 10.5).

**[revised] Number patterns must not eat their neighbours.** Every numeric rule
shares one number pattern, and getting its edges wrong is the most common bug in
this stage. All four of these were real:

- A comma inside a number is a separator; a comma after one is punctuation.
  `2025, and` must not read as the number `2025,`.
- Optional trailing units must not consume the space before them, or
  `$2.50 (12x` loses the space and runs the words together.
- A digit run touching a letter belongs to an identifier. `H100` became
  `H1zero`, and `1990s` became `one hundred ninety-nine0s`, because the pattern
  matched part of a token. Require a non-word character on both sides, and
  handle decades explicitly.
- A unit whose number was consumed by an earlier rule still has to be spoken:
  after the range rule takes `18-52`, the `°C` is on its own.

Group references in `say` use `$1` style. An empty group produces an empty
string, so collapse repeated whitespace after every substitution.

### 9.6 rules/lexicon.yml

One entry per term. This file grows to hundreds of lines over a year. Keep it
sorted by `match` so a `git diff` stays readable.

```yaml
version: 1

defaults:
  case: sensitive          # sensitive | insensitive
  boundary: word           # word | substring

terms:
  - match: AWS
    mode: spell            # "A W S"

  - match: NASA
    mode: word             # leave it alone, the engine says it correctly

  - match: SQL
    mode: respell
    say: sequel
    note: "operator preference, not the only correct reading"

  - match: Kubernetes
    mode: respell
    say: koo-ber-net-ees

  - match: kubectl
    mode: respell
    say: koob-control

  - match: nginx
    mode: respell
    say: engine ex
    case: insensitive

  - match: et al.
    mode: respell
    say: et all

  - match: km/h
    mode: replace
    say: kilometers per hour
    note: "overrides the generic slash rule"

  - match: LLM
    mode: spell

  - match: PyTorch
    mode: respell
    say: pie torch

  - match: Xi Jinping
    mode: respell
    say: shee jin ping
    note: "proper noun, two words, must match as a phrase"
```

Modes:

| Mode | Behavior |
| --- | --- |
| `spell` | Insert a space between letters so the engine reads each letter. |
| `word` | No change. Records the decision so the term stops appearing in `unknowns.jsonl`. |
| `respell` | Replace with the `say` string. This is the main tool. |
| `replace` | Replace with the `say` string. Same mechanism as `respell`, different intent, so the operator can grep the two apart. |
| `ipa` | Only if the backend accepts phoneme input. Fall back to `respell` and log a warning when it does not. |

Note on method: most current neural speech models take text, not phonemes. They
have no reliable phoneme interface. Respelling is therefore the working tool, and
respelling is a matter of ear rather than a matter of standard. Expect the
operator to iterate. Section 9.8 makes that iteration fast.

Multi-word matches must work. `Xi Jinping` above is one entry, not two.

### 9.7 Unknown-term discovery

The pipeline must tell the operator what to tune. Do not make the operator catch
every bug by ear.

After the lexicon step, scan the text for candidate terms with no lexicon entry.
Flag a token when any of these hold:

1. All uppercase, 2 to 6 characters (`GDPR`, `SRE`).
2. Internal capitals (`PyTorch`, `OAuth`, `McKinsey`).
3. Letters and digits mixed (`H100`, `S3`, `GPT-5`).
4. Absent from a bundled English wordlist and absent from a bundled common-name
   list.
5. Three or more consonants in a row and no vowel (`nginx`, `pkgsrc`).

Write each candidate to `unknowns.jsonl` with the term, the count in this
article, and one surrounding sentence.

Add a command:

```
readcast lexicon suggest --since 30d --min-count 2
```

It aggregates across jobs and prints a work queue:

```
term         count  jobs  suggested  example
GDPR            14     6  spell      "...under GDPR, a controller must..."
H100             9     4  respell    "...trained on 512 H100 GPUs..."
Kubernetes       7     3  respell    "...the Kubernetes scheduler..."
```

Never write to `lexicon.yml` automatically. The operator decides and commits.

### 9.8 Audition

Two commands. Both must return in a few seconds. Speed is the whole point.

```
readcast say "koo-ber-net-ees" --play
readcast say --compare "Kubernetes" "koo-ber-net-ees" "cube-er-net-ees" --play
```

Synthesize the strings and play them back to back. The operator picks a
respelling by ear in one pass instead of re-rendering a 40-minute episode.

```
readcast preview <job-id> --minutes 3
```

Re-run preparation and synthesis for the first three minutes only. Write to a
scratch file. Do not touch the feed.

### 9.9 rules/tests.yml

The lexicon grows. Without tests it regresses. Make the tests cheap and make
them part of the build.

```yaml
cases:
  - name: strips parenthetical figure reference
    in:  "The effect was clear (see Figure 2b)."
    out: "The effect was clear."

  - name: keeps inline figure reference
    in:  "Fig. 3 shows the drop."
    out: "Figure 3 shows the drop."

  - name: currency with magnitude
    in:  "Revenue hit $1.2M last year."
    out: "Revenue hit one point two million dollars last year."

  - name: year reads as pairs
    in:  "It shipped in 1984."
    out: "It shipped in nineteen eighty-four."

  - name: lexicon overrides generic slash rule
    in:  "It ran at 90 km/h."
    out: "It ran at ninety kilometers per hour."

  - name: longest match wins
    in:  "The AIX box and the AI model."
    out: "The AIX box and the A I model."

  - name: frozen span is not re-edited
    in:  "The budget was $3M."
    out: "The budget was three million dollars."
```

```
readcast rules test
```

Exit non-zero on any failure. Print a unified diff for each failure. Run this in
CI on the `rules/` repository.

Also keep golden-file tests. Store three saved HTML fixtures under
`tests/fixtures/`. Assert the resulting `spoken.txt` against a committed copy.
This catches extraction regressions, which rule tests cannot see.

## 10. Chunking, synthesis, assembly

### 10.1 Chunking

Split `spoken.txt` on sentence boundaries. Target 200 to 400 characters per
chunk. Never split inside a sentence. Never merge across a heading.

Long inputs make speech models drop or invent words. Small chunks bound that
damage and make a retry cheap.

Tag each chunk with its kind: `intro`, `heading`, `body`, `quote` or `aside`.
**[revised]** The kind decides the voice as well as the instruction, so
blockquote markers must survive preparation into `spoken.txt` — like the heading
marker, kept for the operator and stripped before synthesis. Mind the collision:
if `>` is in the symbols table, a line-leading `> ` must not be read as
"greater than".

### 10.2 The backend interface

Define one protocol. Implement several backends. The operator must be able to
switch with one config line.

```python
class TTSBackend(Protocol):
    name: str
    supports_instruction: bool
    supports_phonemes: bool

    def synth(
        self,
        text: str,
        *,
        voice: str,
        instruction: str | None = None,
        seed: int | None = None,
    ) -> bytes: ...   # 24 kHz mono WAV
```

Ship these backends:

- `mlx` — POST to the local `mlx-audio` server at
  `http://127.0.0.1:8080/v1/audio/speech`. Default model
  `mlx-community/Breeze-TTS-2-mlx` (bf16). Set `supports_instruction = True`.
- `kokoro` — same OpenAI-compatible shape, a different base URL. Fast fallback.
- `elevenlabs` — paid reference.
- `openai` — paid reference.

Use the bf16 weights on a machine with 32 GB or more of memory. The quantized
conversions of this model carry an explicit warning that quantization changes
sampling and audio quality. Quantization removes the prosody the operator is
paying for.

### 10.3a Voice identity

**[new]** An autoregressive TTS model samples a speaker on every request. Ask it
for 184 chunks and you get something close to 184 readers. Measured on one
episode: pitch from 103 to 353 Hz, with 43 of 59 consecutive chunks jumping by
more than 25 Hz. It is the single most annoying thing about the output, and it
is invisible in a short sample.

Fix it by conditioning every request on a **reference clip**: `ref_audio` plus
`ref_text`. One clip per role. Conditioning cut the spread from 50 Hz to 14 Hz
in testing — one speaker with ordinary prosody, rather than a rotating cast.

**[revised] Vary the cast between episodes, never inside one.** A single voice
across the whole feed is correct and dull. Keep the narrator fixed for the
length of an article, and pick a different one for the next: sample a pool of
clips, and give each job the narrator that has read least recently, so two
neighbouring episodes never match. Record the cast on the job row the first time
it synthesizes — a rerender must sound like the episode the listener already
has.

Three roles are enough, and they are cast per episode:

| Role | Reads |
| --- | --- |
| `main` | the intro, headings and body |
| `quote` | blockquotes |
| `aside` | a paragraph that is entirely parenthetical |

Cast the episode when its **text** becomes ready, not when synthesis starts.
The operator wants to hear the narrator and change it while the job is still
waiting, and a cast recorded early is one the rerender can reproduce. Offer the
sample and the re-roll on the queue row itself: playing the new voice
immediately after a re-roll is the whole interaction, because the only way to
judge a voice is by ear. Refuse the change once synthesis is under way.

**Pre-render a pool; roll at random from it.** Generating a clip takes tens of
seconds, so a button that generates on demand feels broken. Sample twenty-odd
voices up front and pick from them instead — indistinguishable from generating
one, and instant.

Use *random* for the operator's re-roll and *least recently used* for automatic
casting. They look like the same choice and are not: an ordered walk through the
pool makes the button feel like a toggle between two voices, while random
automatic casting would eventually put the same narrator on neighbouring
episodes.

**A reference clip carries its own transcript.** Conditioning needs the text to
match the audio, so store the sentence beside the clip. Change the default
wording later and older clips keep the words they actually say. Word that
sentence carefully: it is spoken aloud, and it should claim no more than is
true — a narrator is fixed for one episode, not for the feed.

**Cache anything measured from a clip.** Pitch never changes, and measuring one
costs about eighty milliseconds — trivial once, two seconds across a pool of
twenty-four, which is what the re-roll button paid on every press. Key the cache
by file size and modification time so a re-recording is measured again.

**Report progress from the worker, not from the directory.** Counting chunk
files looks equivalent and is not: a rerender overwrites `000.wav` upward, so
the count sits still while the run is halfway through, and reused chunks are
hardlinks carrying an older render's timestamp. A job that is working normally
then looks wedged. Have the worker write its position after every chunk, and
fall back to counting only when that report goes stale.

**Sampling must take the audio lane's lock.** Two generations at once make the
model babble, so sampling voices while an episode renders quietly corrupts it.

Ship `readcast voices sample`: sample a handful of unconditioned clips, measure
each one's pitch, and assign the ones that sound least alike to the three roles.
The operator auditions and swaps by ear from there. Warn loudly during synthesis
when a role has no clip, because the failure is not an error — it is an episode
that sounds wrong.

The voice is part of the identity of a chunk's audio: include it in the reuse
key from section 10.7, or a re-recorded voice will silently reuse the old one.

**Check the field names against the server.** `mlx-audio` declares `instruct`,
not OpenAI's `instructions`, and has no `seed` parameter at all. Unknown fields
are dropped without complaint, so the per-chunk instructions in section 10.3 did
nothing at all until this was noticed, and the seeded retry was never seeded.

### 10.3 Instruction per chunk kind

When `supports_instruction` is true, pass a short instruction:

```yaml
instructions:
  intro:   "Read as a brief announcement. Neutral and clear."
  heading: "Read as a section heading. Slightly slower, with a falling tone."
  body:    "Read as narration for an audiobook. Calm, even pace."
```

Keep these in `config.yml`. They are tuning knobs.

### 10.4 Verify each chunk

Word dropping is the failure mode that ruins a listen, and it is silent. Catch it.

1. Transcribe the chunk with `mlx-whisper`.
2. Normalize both the transcript and the input text (lowercase, strip
   punctuation, **and spell numbers out**).
3. Compute the character error rate.
4. If the rate is above 0.15, re-synthesize with a new seed. Retry twice.
5. If it still fails, keep the best attempt, increment `flagged_chunks`, and note
   the chunk index in the episode description.

Make the threshold and the retry count configurable. Allow `--no-verify` for
speed.

**[revised] Two corrections, both learned the expensive way.** Verification
itself is nearly free — whisper transcribes a 24-second chunk in under half a
second, about 1% on top of synthesis. What costs real time is a *retry*, because
a retry is a whole re-synthesis. So a false positive is expensive, and the first
implementation produced them constantly:

- **Whisper writes numbers as numerals.** It hears "thirty-eight percent" and
  transcribes "38%". Comparing that against the spelled-out text scores every
  number in the article as an error. Both sides must go through the same number
  handling first.
- **Character error rate is meaningless on a short string.** A one-word heading
  is eleven characters; one mis-heard syllable scores 0.45, and whisper is
  unreliable on a one-second clip regardless. Skip the check below
  `verify.min_chars` (40). Dropped words are a long-input problem.

Fixing both took one article from 9 retries to 2, and its real-time factor from
0.18 to 0.32 — 35 minutes of wall clock down to 20.

### 10.5 Per-domain overrides

`rules/domains/<host>.yml` merges over the global rules for that host only.

```yaml
version: 1
extract:
  prefer_client_html: true      # this site blocks server-side fetch
  drop_selectors:
    - ".newsletter-inline"
    - ".author-bio"
strip:
  patterns:
    - id: site-promo
      match: 'Subscribe to .* for more'
lexicon:
  terms:
    - match: Ars
      mode: respell
      say: arse
```

Merge order: global, then domain. Domain entries with the same `id` or `match`
replace the global entry. Log every override that fires.

### 10.6 Assembly

1. Concatenate the chunk WAV files in order.
2. Insert silence: 500 ms at a paragraph break, 1200 ms before a heading, 1000 ms
   after the intro.
3. Prepend a spoken intro built from a template in `config.yml`:
   `"{title}. From {publication}. By {author}. Published {published_at:%B %-d, %Y}."`
   Omit any field that is missing. Do not say "unknown author".
4. Normalize loudness with a two-pass `ffmpeg` `loudnorm` to `I=-16 LUFS`,
   `TP=-1.5 dBTP`, `LRA=11`. This is the podcast convention. Without it the
   episode sits at a different volume from every other show in the queue.
5. Encode to MP3, mono, 64 kbps, 24 kHz.
6. Write ID3 tags with `mutagen`:
   - `TIT2` title, `TPE1` author, `TALB` `"readcast"`, `TDRC` publish date.
   - `WOAS` and a `COMM` frame holding the source URL.
   - `APIC` cover art from `data/feed/cover.jpg`.
   - `CHAP` and `CTOC` frames, one chapter per heading, so the listener can skip
     a section.

### 10.7 Resuming a long render

**[revised]** A 40-minute article is a two-hour render. Losing it to a restart,
a crash or a one-line edit is not acceptable, and it happens often enough during
tuning to matter.

Write each finished chunk with a sidecar holding the exact text that produced it,
and keep a copy keyed by the hash of that text under `chunks/.cache/`. On every
synthesis run, reuse any chunk whose text already has audio.

Key it by **text, not by chunk index**. Editing one paragraph shifts the index of
every chunk after it; index-keyed reuse would re-render the entire rest of the
article for a one-word change. Hardlink rather than copy, so reuse costs no disk.

An interrupted render then resumes where it stopped, and an edit costs only the
chunks that actually changed.

## 11. Feed

Write `data/feed/feed.xml` after every successful job. Regenerate the whole file.
Do not patch it.

Requirements:

- RSS 2.0 with the `itunes` namespace.
- Serve at `/f/<token>/feed.xml`. The token is 32 random URL-safe characters.
  Most podcast apps cannot send credentials, so **the URL is the credential**.
  State this in the README. Support `readcast feed rotate` to issue a new token.
- `<itunes:block>yes</itunes:block>` on the channel. Send
  `X-Robots-Tag: noindex` on both the feed and the audio.
- One `<item>` per successful job.
- `<guid isPermaLink="false">` equals the job id. Never change it.
- `<pubDate>` equals `submitted_at`, not the article publish date. The listener
  wants the most recently saved item at the top.
- `<enclosure>` `length` must equal the exact byte size on disk. A wrong length
  breaks download progress in several clients.
- `<itunes:duration>` in seconds.
- `<description>` holds the source link, the author, the estimated word count,
  and a flagged-chunk warning when `flagged_chunks > 0`.
- Keep the newest `feed.max_items` entries, default 100. Keep the audio files on
  disk regardless.
- Serve audio with `Accept-Ranges: bytes`. Podcast clients issue range requests.

## 12. Submit paths

### 12.1 API

```
POST /jobs
  {"url": "...", "title": "...", "client_html": "...", "backend": "mlx", "voice": "..."}
  -> 202 {"id": "2026-09-09-a1b2c3"}

POST /jobs/form          -> form-encoded submit for pages whose CSP blocks fetch
GET  /jobs/{id}          -> job row plus stage timings
GET  /jobs?state=failed  -> list
POST /jobs/{id}/rerender?from=preparing  -> re-run from a stage, same id
POST /jobs/{id}/hold[?release=true]      -> keep a ready job out of the audio lane
DELETE /jobs/{id}        -> remove from feed, keep files
GET  /status             -> read-only HTML page, last 50 jobs
GET  /f/{token}/feed.xml            (GET and HEAD)
GET  /f/{token}/audio/{id}.mp3      (GET and HEAD, byte ranges)

# the dashboard, section 12.4
GET  /ui                            -> the page itself, no token
GET  /api/state                     -> everything the dashboard draws
POST /api/queue/order  {"order": [ids]}   -> reorder the audio queue
GET  /api/jobs/{id}/text            -> spoken.txt
PUT  /api/jobs/{id}/text  {"text": "..."} -> replace it
```

**[revised]** Answer `HEAD` on the feed and the audio. Podcast clients check
size and type before they download, and a 405 there breaks them.

Authenticate `POST /jobs` with a static bearer token from `config.yml`. Enable
CORS for that route and handle the `OPTIONS` preflight.

`rerender` is the tuning loop. The operator edits a rule, runs
`rerender?from=preparing`, and the same episode reappears with the fix. Keep the
same job id so the podcast client replaces the file rather than adding a
duplicate. Bump a `?v=` query parameter on the enclosure URL to defeat client
caching.

### 12.2 Bookmarklet

Build this first. It needs no store listing, no signing, and no review. Generate
it from a template and print it with `readcast bookmarklet`.

```js
javascript:(async()=>{
  const r=await fetch('https://HOST/jobs',{
    method:'POST',mode:'cors',
    headers:{'content-type':'application/json','authorization':'Bearer TOKEN'},
    body:JSON.stringify({
      url:location.href,
      title:document.title,
      client_html:document.documentElement.outerHTML.slice(0,4e6)
    })});
  const t=document.createElement('div');
  t.textContent=r.ok?'readcast: queued':'readcast: failed';
  t.style.cssText='position:fixed;top:12px;right:12px;z-index:2147483647;padding:8px 12px;background:#111;color:#fff;font:13px system-ui;border-radius:6px';
  document.body.appendChild(t);setTimeout(()=>t.remove(),2500);
})()
```

Sending `client_html` is the point. The page is already rendered and the
operator is already logged in.

The host must be HTTPS. A browser blocks a plain HTTP request from an HTTPS page
unless the target is `localhost`. Section 13 solves this.

### 12.3 Extension

**[revised] Build this, and treat it as the primary path.** The claim that an
extension "does not buy any new capability" was wrong.

A bookmarklet's `fetch` runs in the page's own origin and therefore obeys that
page's `Content-Security-Policy`. Large sites forbid connecting anywhere but
their own hosts, so on Wikipedia the request never reaches the network at all:

```
Refused to connect to 'http://127.0.0.1:8788/jobs' because it violates
the document's Content Security Policy.
```

An extension service worker's fetch is not subject to the page's CSP. That is a
new capability, and it is the difference between working everywhere and working
on some sites.

Keep the bookmarklet as the zero-install option, and give it a fallback: when
`fetch` throws, submit a hidden form to `POST /jobs/form` instead. CSP's
`form-action` has no `default-src` fallback, so the form survives where the fetch
cannot. A site that sets `form-action` explicitly blocks that too — which is the
point at which the operator wants the extension.

Manifest V3, one action button, one options page for host and token,
`activeTab` rather than a blanket host permission. Two things the options page
must get right, both of which were wrong first time:

- **Save on every edit**, not only on a button click. Typing a token and pressing
  Enter is the natural gesture, and it stored nothing.
- **Show what is actually stored.** Extension storage is per Chrome profile, so
  a second profile is a second setup, and "it keeps asking for the token" is the
  symptom. Say so on the page, and offer a one-paste setup link
  (`options.html#host=…&token=…`) so configuring another profile is not typing.

### 12.4 The dashboard

**[new]** A single page at `/ui`, served without a token; it asks the browser for
one and keeps it in `localStorage`. Self-contained: no external requests.

It shows what section 15's terminal dashboard shows — services, the running job
with a progress bar and an ETA, the queue, recent jobs — and adds the two things
that need a mouse:

1. **Reorder the audio queue.** Drag, or use per-row up and down buttons; drag
   alone is awkward from a keyboard and absent on touch. Order is stored as
   `queue_rank`, and the audio lane claims by it, then by arrival.
2. **Offer only what will happen.** A control that does nothing is worse than a
   missing one, and a control that quietly spends two hours of the machine is
   worse still. Show hold and release only for a job actually waiting in the
   queue; give a finished episode a separate re-render that states its cost and
   asks first. Beware naming a button after a CLI command that does more than it
   does.
3. **Read and edit the prepared text.** Click a title, get `spoken.txt` in a
   textarea, save it back. This is the point of the `ready` pause: the operator
   reads what the engine will be given and fixes it before paying for the audio.
   Refuse the edit with 409 while the audio lane holds that job — it has already
   read the file.

## 13. Hosting and access

State these constraints plainly in the README, because they decide whether the
system works at all.

1. **The host must stay awake.** A laptop that sleeps cannot answer when the
   phone refreshes the feed at 6 a.m. Run this on an always-on machine.
2. **Use Tailscale Serve.** It gives the service a real HTTPS hostname with a
   valid certificate on the tailnet. That single step fixes three problems: the
   bookmarklet mixed-content block, remote access without opening a port, and a
   feed URL the phone can reach.
3. **The podcast app must fetch the feed on the device.** An app that refreshes
   feeds on the vendor's own servers cannot reach a private tailnet address.
   AntennaPod fetches on the device. Test the chosen app before committing.
4. Run the API and the `mlx-audio` server as two `launchd` agents with
   `KeepAlive`. Ship both plists in `deploy/`.

## 14. Backend comparison

The operator has an open decision between local speech and a paid service. Make
the comparison a command, not a project.

```
readcast compare <url> --backends mlx,elevenlabs --minutes 4
```

Prepare the text once. Synthesize the first four minutes with each backend.
Write `compare/<job-id>/<backend>.mp3`. Print a table of real-time factor,
wall-clock seconds, flagged chunks, and estimated cost per episode at full
length.

Use one full article, not a short sample. A short sample is where every backend
sounds fine.

## 15. CLI

```
readcast serve
readcast add <url> [--backend B] [--voice V]
readcast jobs [--state S]
readcast rerender <id> [--from STAGE]
readcast preview <id> [--minutes N]
readcast say <text> [--compare ...] [--play]
readcast rules test
readcast lexicon suggest [--since 30d] [--min-count 2]
readcast compare <url> --backends A,B [--minutes N]
readcast feed rotate | rebuild | url
readcast bookmarklet [--base URL]

# added while living with it
readcast watch                      # terminal dashboard; 1-9 opens a job's text
readcast show <id> [--chunks|--raw] # the text, or the exact per-call strings
readcast prep <url|id>              # text only, stop at ready
readcast hold <id> / release <id>   # keep a job out of the audio lane, or send it
readcast ui                         # the dashboard URL, token filled in
readcast extension-link <ext-id>    # one-paste setup for a Chrome profile
readcast init
```

**[revised]** `uv pip install -e .` puts the CLI in `.venv/bin`, which is not on
`PATH`, and the CLI finds `config.yml` by walking up from the working directory.
A bare symlink therefore works inside the project and fails everywhere else.
Document the wrapper that pins `READCAST_CONFIG`.

## 16. config.yml

```yaml
base_url: https://readcast.tailnet-name.ts.net
api_token: CHANGE_ME
data_dir: ./data
rules_dir: ./rules

tts:
  backend: mlx
  voice: default
  mlx_url: http://127.0.0.1:8080/v1   # check the port is free; 8080 is popular
  model: mlx-community/Breeze-TTS-2-mlx
  instructions:
    intro: "Read as a brief announcement. Neutral and clear."
    heading: "Read as a section heading. Slightly slower, with a falling tone."
    body: "Read as narration for an audiobook. Calm, even pace."

pipeline:
  hold_for_review: false   # true: every job waits for `readcast release <id>`

chunk:
  target_chars: 300
  max_chars: 450

verify:
  enabled: true
  max_cer: 0.15
  retries: 2
  min_chars: 40            # below this the check is noise, not signal

audio:
  lufs: -16
  bitrate_kbps: 64
  pause_paragraph_ms: 500
  pause_heading_ms: 1200

feed:
  title: readcast
  author: ""
  max_items: 100
  intro_template: "{title}. From {publication}. By {author}."
```

## 17. Milestones

Build in this order. Each milestone must run before the next one starts.

**M1 — text on disk.** `POST /jobs` with a URL. Fetch, extract, write
`extracted.md`. No audio. Acceptance: three different sites produce clean
markdown with headings intact.

**M2 — a subscribable feed.** Add naive chunking, the `mlx` backend, assembly,
and `feed.xml`. Skip all rules. Acceptance: subscribe in AntennaPod over
Tailscale and play an episode end to end.

**M3 — the rules engine.** Add strip, normalize, lexicon, the frozen-span mask,
`transforms.jsonl`, and `rules test`. Acceptance: every case in section 9.9
passes, and `spoken.txt` contains no bracketed citation and no bare `$1.2M`.

**M4 — the one-click path.** Bookmarklet, `client_html`, Tailscale Serve, and the
`launchd` agents. Acceptance: one click on a paywalled article the operator can
read produces an episode.

**M5 — the tuning loop.** `unknowns.jsonl`, `lexicon suggest`, `say`, `preview`,
`rerender`. Acceptance: the operator hears a wrong pronunciation, finds the term
with `lexicon suggest`, tests a respelling with `say`, commits the rule, and
runs `rerender` on the affected episode.

**M6 — quality controls.** Chunk verification, chapter marks, loudness
normalization, `compare`. Acceptance: `compare` prints a real-time factor and a
cost estimate for two backends on the same article.

**M7 — review before you render.** **[new]** The text lane, the `ready` state,
chunk reuse keyed by text, and the web dashboard at `/ui`. Acceptance: an article
submitted while a two-hour render is running has readable text within seconds;
its text can be edited in the browser and the edit is what gets spoken; the queue
can be reordered; and a render interrupted halfway resumes without redoing the
chunks it already made.

## 18. Acceptance tests

Write these as automated tests where possible.

1. A 6000-word article completes and produces a single MP3 with correct duration
   metadata.
2. `spoken.txt` for the fixture articles contains no `[`, no `Fig.`, no `$`, no
   `%`, and no `http`.
3. `rules test` exits zero.
4. Deleting `feed.xml` and re-running `readcast feed rebuild` reproduces a
   byte-identical file.
5. Two jobs submitted at the same time run one after the other, never together.
6. A job that fails at extraction does not appear in `feed.xml`.
7. `rerender --from preparing` keeps the job id and the guid, and changes the
   enclosure `?v=` value.
8. The enclosure `length` matches the byte size on disk for every item.
9. A range request for bytes 100 to 200 of an episode returns HTTP 206.
10. Rotating the feed token makes the old feed URL return HTTP 404.

**[new]**

11. A job submitted while the audio lane is busy reaches `ready`, with
    `spoken.txt` on disk, without waiting for that lane.
12. Text edited through `PUT /api/jobs/{id}/text` is what appears in the chunk
    plan; editing is refused with 409 while the job is synthesizing.
13. Reordering the queue changes which job the audio lane claims next.
14. A second synthesis run over unchanged text makes no synthesis calls, and
    inserting a paragraph re-renders only the inserted chunk.
15. A worker that starts while another holds the lock waits for it, then reaps
    any job the previous process left mid-flight.
16. A database created by an older version gains the new columns on open, with
    its existing rows intact.
17. `spoken.txt` contains no `*`, no `<`, and no raw HTML tag.
18. `HEAD` on the feed and on an episode returns 200 with a correct
    `Content-Length`.

## 19. What it actually costs

**[new]** Measured, so the numbers are not a surprise later.

Breeze TTS 2 is an autoregressive LLM, not a vocoder: a Qwen3 backbone of about
1.7B parameters, a 12-layer depth decoder, 16 codebook tokens per audio frame,
and a codec at 25 frames per second. That is 25 backbone passes plus 400 depth
passes — **425 sequential forward passes per second of speech** — at batch size
one, in bf16.

On an M-series Mac that lands at **RTF 0.3 to 0.4**: roughly three minutes of
compute per minute of audio. A 6,000-word article is a two-to-three hour render.
Every design decision above about resuming, reordering, and reviewing text before
synthesis follows from that number.

Two things that look like levers and are not:

- **Concurrency corrupts.** Three overlapping requests to `mlx-audio` returned
  73 seconds of audio for text that takes 22 sequentially: the model babbles when
  requests interleave. Keep the audio lane strictly serial.
- **Verification is not the cost.** It is about 1% of synthesis. Retries are the
  cost, which is why false positives matter so much (section 10.4).

The real lever is the backend: `kokoro` is non-autoregressive and many times
faster, with less prosody. Quantized Breeze weights would also help, and the
model card warns that quantization changes the audio quality that is the reason
for running it locally.

## 19b. Estimating what is left

**[new]** A two-hour render needs an honest progress estimate, and the naive one
is wrong in a specific way worth writing down.

Do not divide elapsed time by the number of chunk files on disk. Reused chunks
(section 10.7) are hardlinks and keep the modification time of the render that
made them, so that arithmetic counted work never done in this run: it reported
19.6 seconds per chunk where the truth was 52.

Instead:

1. Seed with a prior — a seconds-per-chunk figure measured on this machine,
   persisted in the settings table and refined after every job. Ship a default
   for the first run.
2. Measure only chunks written since the current run's plan file.
3. Fold each observed gap into a decaying average, so recent chunks weigh more
   than old ones.
4. Clamp outliers rather than dropping them. A retry or a paused machine should
   nudge the estimate, not redefine it. Ignore anything under a second: that is
   a reused chunk, and it proves nothing about speed.

Show the result as a duration **and as a clock time**. "Two hours left" and
"done at 22:29" are different questions, and at these render times the operator
is asking the second one.

## 19c. Two backends, chosen per article

**[new]** Once a fast backend exists alongside a good one, the choice belongs to
the article, not to the feed. Offer it on the queue row.

Three things this turns up:

- **Do not stamp the default backend on a job at submission.** Leave it unset
  and resolve it when the job renders, or changing the default silently fails to
  move everything already queued. Record what actually produced the audio at
  synthesis, which is what section 20's provenance needs anyway.
- **Backends do not share speakers.** One clones a voice from a reference clip,
  the other names a voice pack. Switching must clear the cast, and a stored
  speaker that means nothing to the current backend must not be displayed.
- **Learn the pace per backend.** They are two orders of magnitude apart, so a
  single average gives a six-hour estimate to a ten-minute render for as long as
  it takes to converge.

## 19d. Stopping and removing

**[new]** A render can run for hours, so the operator must be able to stop one,
and a queue is only a queue if things can leave it.

- **Stopping is not failing.** Give it its own state. Keep every chunk already
  produced, so releasing the job again resumes rather than restarts, and keep it
  out of the feed either way.
- **Check the stop flag between chunks**, never inside one: prompt enough at any
  chunk size, and it can never leave a half-written file behind.
- **Refuse to remove a job that is rendering.** Ask for it to be stopped first,
  rather than deleting the directory out from under the worker.
- Deleting the row is not the same as deleting the audio. Make the difference
  explicit and default to keeping the files.

## 19f. Reading a PDF

**[new]** A PDF is a description of marks on a page. There is no document
structure to extract, so paragraphs, headings and the boundary between the
article and its furniture all have to be recovered.

Four artifacts are present in every real paper and each one is audible:

1. **A word broken across a line break** reads as two words. Decide splits
   against the bundled wordlist, not by a rule about capitals: `transduction`
   is a word so `transduc-/tion` rejoins, while `well` and `known` are both
   words so `well-/known` keeps its hyphen.
2. **Page numbers and running heads** are read aloud as prose. A line repeated
   at the top or bottom of several pages is furniture — but count each line
   once per page, or on a short page the top and bottom slices overlap and a
   body line looks repeated.
3. **Front matter** — licence grants, author lists, affiliations, emails — is
   not the paper. Start at the abstract.
4. **The reference list** is around a quarter of a paper and unlistenable.

Recover headings from numbered sections (`3.2 Attention`) and known section
names, and isolate them with blank lines *before* unwrapping line breaks into
paragraphs — otherwise the heading swallows the paragraph after it.

The title deserves its own care: a first page opens with a licence grant or a
submission stamp as often as with the title, and a line that begins in lower
case is the middle of a wrapped sentence rather than a heading.

Two more traps, both from real papers:

- **Ligatures.** `ﬁ`, `ﬂ` and `ﬃ` arrive as single glyphs. They reach the engine
  as characters it has never seen, and they defeat every wordlist lookup — so
  resolve them *before* de-hyphenation, or `task-/speciﬁc` cannot be recognised
  as a compound.
- **Titles come from two unreliable places.** Many PDFs carry no `/Title` at
  all, and one that disagrees with the first page is worth distrusting. Prefer
  the page when the two conflict, but never discard metadata for nothing: only
  swap one title for another. Reading the page has its own traps — the first
  lines are often a licence grant, a line beginning in lower case is the middle
  of a wrapped sentence, and a title that ends on a preposition continues onto
  the next line while one that does not is followed by an author.

Two failure modes to name rather than let the operator guess at:

- **A PDF captured by the browser extension is an empty viewer shell.** Notice
  it, discard it, and fetch the document.
- **A scanned PDF has no text layer.** Say so; do not produce an empty episode.

## 19e. Not built: a commentary companion

**[proposed]** An episode of commentary on each article — hand the prepared text
to an LLM, have it critique the piece and add context where it is warranted, and
publish that as a **separate item** in the feed.

Recorded here rather than built, with the reasoning, so the decision does not
have to be made again from scratch.

**It sits closer to section 2 than it first appears.** The non-goal there is
summarization, rewriting, and the two-host "AI podcast" format. A separate
episode leaves all of that intact: the article is still read in full, in order,
unaltered. The commentary is an addition beside it, not a transformation of it.
That distinction is the whole reason this is admissible at all — if it ever
starts editing the article, it has become the thing section 2 forbids.

**It breaks "no cloud dependency in the default path", and that must be kept.**
Sending each article to a hosted model means the operator's reading — including
anything paywalled that `client_html` carried through on their session — leaves
the machine. That is a legitimate choice and an illegitimate default. So:

1. Off unless explicitly enabled.
2. The article render never depends on it. No key, no network, or a refusal
   means no companion episode and nothing else changes.

**Shape, if it is built:**

- A companion job, `<parent-id>-note`, with its own guid, its own MP3 and its
  own feed item — titled so a podcast client shows it as a skippable episode
  next to the article, not as part of it.
- It reads `spoken.txt`, so the model sees the same cleaned prose the engine
  does, with no markup to trip on.
- The response goes through the ordinary pipeline: rules, chunking, synthesis.
  It is text like any other, and the operator can read and edit it before it
  renders, exactly as with an article.
- A **different narrator** from the article's, so the listener never mistakes
  commentary for the author.
- The generated text is written to disk beside the article's, because the
  property in section 1 applies to it too: whatever reaches the speech engine is
  a durable, editable file.

## 20. License note

The Breeze TTS 2 weights carry a research and non-commercial license. The
inference code is Apache 2.0. A personal feed is fine. Any client-facing or
revenue-generating use needs a separate license from the vendor, or a different
backend. Record the active backend and model on every job row so the provenance
of any given episode stays clear.