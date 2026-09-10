# readcast

Save an article from the browser. Get a listenable episode in any podcast app a
few minutes later.

Self-hosted, single user, no cloud dependency in the default path. One click in
the browser starts the work; the audio lands in a private feed on your tailnet.

Two properties matter more than anything else, and everything else here exists
to support them:

1. **The text that reaches the speech engine is a durable, editable file.**
   `spoken.txt` is on disk for every job. Read it, find the bug, fix the rule,
   re-render that one episode.
2. **Pronunciation and cleanup rules live in version control.** `rules/` is
   meant to be its own git repository. `git diff` is the review tool.

No summarization. No LLM rewrite. No two-host "AI podcast" format. The article,
in full, in order.

---

## What it does

```
browser bookmarklet ──HTTPS POST──┐
curl / CLI ───────────────────────┤
                                  ▼
                          FastAPI  /jobs
                                  │
                                  ▼
                       SQLite job queue (1 worker)
                                  │
   ┌──────────────────────────────┼──────────────────────────────┐
   ▼            ▼          ▼      ▼      ▼         ▼             ▼
 fetch  →  extract  →  prepare  →  chunk  →  synth  →  assemble  →  publish
   │            │          │                   │         │            │
raw.html  extracted.md  spoken.txt        chunks/*.wav episode.mp3  feed.xml
                        transforms.jsonl
                        unknowns.jsonl
```

Every stage writes its output under the job directory and reads only the
previous stage's file, so any stage can be re-run on its own.

## Requirements

- macOS on Apple silicon, 32 GB of memory or more for the local speech model.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- `ffmpeg` and `ffprobe` on `PATH` (`brew install ffmpeg`).
- An always-on machine. See [Hosting](#hosting), which decides whether this
  works at all.

## Install

One command, from a fresh clone to a running system:

```bash
./setup.sh
```

It checks prerequisites, installs dependencies, writes a `config.yml` with a
freshly generated `api_token`, creates the data directory and feed, starts the
API and the speech server as launchd agents, and records the sample voices. It
is idempotent — run it again after a `git pull` and it does only what is
missing. It will not overwrite an existing `config.yml`.

```bash
./setup.sh --base-url https://readcast.your-tailnet.ts.net   # skip LAN-IP guessing
./setup.sh --no-mlx        # no local speech engine (use a hosted backend)
./setup.sh --no-services   # do not touch launchd
./setup.sh --no-voices     # skip the multi-gigabyte model download
./setup.sh --help
```

The rest of this section is what `setup.sh` does, for when you want to do it
by hand or a step needs unpicking.

```bash
uv sync
uv pip install -e .
```

Optional extras:

```bash
uv sync --extra browser   # Playwright fallback for pages that resist extraction
uv sync --extra mlx       # mlx-audio (speech) and mlx-whisper (verification)
uv run playwright install chromium
```

Then make your own config. `config.yml` is gitignored — it holds the token and
the URL your phone reaches, so it never leaves this machine:

```bash
cp config.example.yml config.yml
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # your api_token
```

Edit the two lines at the top:

```yaml
base_url: https://readcast.your-tailnet.ts.net   # the URL the phone will use
api_token: <the string you just generated>       # not CHANGE_ME
```

`readcast` refuses to accept jobs while `api_token` is still `CHANGE_ME`.

```bash
uv run readcast init          # creates data/, a placeholder cover, an empty feed
uv run readcast rules test    # 20 cases, should print "0 failed"
```

### Putting `readcast` on your PATH

`uv pip install -e .` puts the CLI in `.venv/bin`, which is not on your PATH,
and the CLI finds `config.yml` by walking up from the current directory — so a
bare symlink works inside the project and fails everywhere else. A wrapper
fixes both:

```bash
cat > ~/.local/bin/readcast <<'SH'
#!/bin/sh
: "${READCAST_CONFIG:=$HOME/Developer/readcast/config.yml}"
export READCAST_CONFIG
exec "$HOME/Developer/readcast/.venv/bin/readcast" "$@"
SH
chmod +x ~/.local/bin/readcast
```

Override the config per invocation with `READCAST_CONFIG=... readcast jobs`.

### Recreating this on another machine

The repository is about 1.4 MB. It carries the code, the rules, the browser
extension, the launchd plists and `uv.lock` — everything needed to rebuild the
setup, and nothing that is machine-specific or large.

What git deliberately does not carry, and how each comes back:

| Not in git | Why | How to get it back |
| --- | --- | --- |
| Model weights (Breeze, Kokoro, Whisper) | Gigabytes; they belong to HuggingFace's cache, not a project | Downloaded on first synthesis into `~/.cache/huggingface` |
| `config.yml` | Holds `api_token` and your private `base_url` | `cp config.example.yml config.yml`, then generate a new token |
| `data/` | Jobs, audio, `readcast.db`, the feed, logs | `uv run readcast init` |
| `data/voices/` clips | Sampled, not authored | `uv run readcast voices init` — Breeze samples fresh speakers, so they will sound different but equivalent. Kokoro needs no clips at all; its voices are named packs listed in the config. |
| `.venv/` | 1.6 GB of resolved wheels | `uv sync` reproduces it exactly from `uv.lock` |

So a clean checkout is:

```bash
git clone <your-repo-url> readcast && cd readcast
./setup.sh
```

The feed token is regenerated by `readcast init`, so the subscription URL on
your phone changes. Re-add the feed once and it is stable from then on.

## The speech server

The speech model runs as a **separate process**. It holds several gigabytes and
must not be imported into the API process.

```bash
uv run python -m mlx_audio.server --host 127.0.0.1 --port 8080
```

Expect roughly **0.4× real time** for Breeze TTS 2 bf16 on an M-series Mac with
verification on: about 2.5 minutes of compute per minute of audio, so a
6,000-word article is a two-to-three hour render. Queue long pieces and walk
away, or switch `tts.backend` to `kokoro` when you want speed over prosody.

Use the **bf16** weights of `mlx-community/Breeze-TTS-2-mlx`. The quantized
conversions carry an explicit warning that quantization changes sampling and
audio quality — that is the prosody you are running a local model to get.

> **Port 8080 is a popular default.** Check it is free before you start:
> `lsof -nP -iTCP:8080 -sTCP:LISTEN`. If something else has it, pick another
> port and change `tts.mlx_url` in `config.yml` to match.

Switch backends with one line in `config.yml` (`tts.backend`): `mlx`, `kokoro`,
`elevenlabs`, or `openai`. The paid ones read their keys from
`ELEVENLABS_API_KEY` / `OPENAI_API_KEY`.

### Fast or good: measured on this machine

| | RTF | A 785-word article | Speakers |
| --- | --- | --- | --- |
| `mlx` — Breeze TTS 2 | **0.32** | 35 minutes | any, cloned from a reference clip |
| `kokoro` — Kokoro 82M | **28.6** | 23 seconds | 54 fixed voice packs |

Roughly ninety times the wall clock for the same article. Breeze is an
autoregressive 1.7B model emitting 425 forward passes per second of speech;
Kokoro is a non-autoregressive 82M model that does it in one pass. Which sounds
better is a matter of ear — render the same piece both ways and listen.

Each queued episode carries a **fast / good** switch in the dashboard, so the
choice is per article rather than per feed — a long piece you want in the
morning can go fast while something you care about takes the slow path. The two
backends share no speakers, so switching recasts the narrator. A job records the
backend that actually produced it only once it renders; until then it follows
whatever `tts.backend` says.

Kokoro needs its phonemizer: `uv pip install "misaki[en]"`. It takes named
voices rather than reference clips, so it never wanders, and a per-episode
narrator means a different name from `tts.backends.kokoro.voices` rather than a
recorded clip. Everything else — roles, re-rolling, the reuse cache — works the
same.

## Use it

```bash
readcast serve                       # API and the one worker
readcast add https://example.com/an-article
readcast jobs
readcast bookmarklet                 # print the bookmarklet, drag it to the bar
```

Subscribe in a podcast app to the URL from `readcast feed url`.

### Getting articles in: the button

Two ways in. **Use the extension.** The bookmarklet is the zero-install option
and it does not work everywhere — that is a browser rule, not a bug in readcast.

#### The extension (recommended)

```
chrome://extensions → Developer mode → Load unpacked → pick ./extension
```

Then open its options page and paste your host (`http://127.0.0.1:8788`) and the
`api_token` from `config.yml`. Click the toolbar button on any article, or press
`Cmd+Shift+U`.

It sends the page's **already-rendered HTML** along with the URL. That is how a
paywalled or JavaScript-heavy article gets through: the browser has already run
the page and already holds your session, so the server never has to log in as
you. `client_html` is capped at 4 MB; a larger body is rejected with 413.

It asks for `activeTab`, `scripting`, `storage` and `notifications` — no blanket
"read your data on all websites". The page is only read on the tab you click.

#### The bookmarklet, and why it is second

`readcast bookmarklet --base http://127.0.0.1:8788` prints a one-line bookmark.
Drag it to the bookmarks bar.

A bookmarklet's `fetch` runs **in the page's own origin**, so it obeys that
page's `Content-Security-Policy`. Many large sites forbid connecting anywhere
but their own hosts. On Wikipedia the request never reaches the network:

```
Refused to connect to 'http://127.0.0.1:8788/jobs' because it violates
the document's Content Security Policy.
```

So the bookmarklet tries `fetch` first and falls back to submitting a hidden
form to `POST /jobs/form`, which opens a small confirmation tab that closes
itself. CSP's `form-action` has no `default-src` fallback, so the form survives
on sites where `fetch` cannot. A site that sets `form-action` explicitly will
block that too — which is the point at which you want the extension.

The form path carries the token as a field, because a form cannot set an
`Authorization` header. Same secret, same exposure as the bookmarklet itself.

#### Why the host must be loopback or HTTPS

A browser blocks an HTTPS page from talking to plain HTTP, with one exception:
`127.0.0.1` and `localhost` count as trustworthy origins. So either point the
button at loopback, or put readcast behind Tailscale Serve and point it at the
`https://` name. `--base` exists for exactly this split: the browser posts to
loopback while `base_url` stays the address your **phone** uses for the feed.

## Hosting

These constraints decide whether the system works at all.

1. **The host must stay awake.** A laptop that sleeps cannot answer when the
   phone refreshes the feed at 6 a.m. Run this on an always-on machine.
2. **Use Tailscale Serve.** It gives the service a real HTTPS hostname with a
   valid certificate on your tailnet, which fixes three problems at once: the
   bookmarklet's mixed-content block (a browser will not let an HTTPS page POST
   to plain HTTP), remote access without opening a port, and a feed URL the
   phone can actually reach.

   ```bash
   tailscale serve --bg 8000
   tailscale serve status          # copy the https://<host>.ts.net URL
   ```

   Put that URL in `base_url`, then re-run `readcast bookmarklet` — the old one
   has the old host in it.
3. **The podcast app must fetch the feed on the device.** An app that refreshes
   feeds on the vendor's servers cannot reach a private tailnet address.
   AntennaPod fetches on the device. Test your app before committing to it.
4. **Run both processes under launchd** with `KeepAlive`:

   ```bash
   ./deploy/install.sh             # writes both plists with this path baked in
   launchctl list | grep readcast
   ```

### The feed URL is the credential

Most podcast apps cannot send an `Authorization` header, so the 32-character
token in the feed path is the only gate on the feed and the audio. Treat the URL
as a secret. Both carry `X-Robots-Tag: noindex`, and the channel carries
`<itunes:block>yes</itunes:block>`.

If it leaks:

```bash
readcast feed rotate      # old URL starts returning 404; resubscribe
```

## Tuning, which is the actual work

The rules are the product. Expect to edit them for months.

| File | What lives there |
| --- | --- |
| `rules/strip.yml` | What gets deleted: code, tables, citations, figure refs, boilerplate. |
| `rules/normalize.yml` | Numbers, currency, dates, ranges, percent, units, URLs, symbols, plus your own regex. |
| `rules/lexicon.yml` | Per-term pronunciation. Sorted by `match` so diffs stay readable. |
| `rules/tests.yml` | Cases that must keep passing. |
| `rules/domains/<host>.yml` | Overrides for one site, merged over the global rules. |

Order of operations, once, in a single pass: structural strip → pattern strip →
pre-builtin rules → builtins → post-builtin rules → lexicon.

**The frozen-span rule.** When a rule replaces a span, the replacement is
frozen and no later rule may match inside it. Without it a currency rule turns
`$1.2M` into `one point two million dollars` and a units rule then finds an `M`
and edits the result again. Lexicon terms are reserved *before* the builtins
run, which is how `km/h` outranks the generic slash rule and still becomes
`kilometers per hour`.

### The loop

```bash
readcast lexicon suggest --since 30d --min-count 2   # what to fix, ranked
readcast say --compare "Kubernetes" "koo-ber-net-ees" "cube-er-net-ees" --play
# edit rules/lexicon.yml, then:
readcast rules test
readcast preview <job-id> --minutes 3                # scratch file, feed untouched
readcast rerender <job-id> --from preparing          # same id, same guid
git -C rules commit -am "kubernetes respelling"
```

`lexicon suggest` never writes to `lexicon.yml`. You decide and you commit.

A rerender keeps the job id and the guid, so the podcast client replaces the
episode instead of adding a duplicate, and bumps `?v=` on the enclosure URL to
defeat client caching.

### Reading the artifacts

- `spoken.txt` — exactly what the engine was given. Start here.
- `transforms.jsonl` — one line per applied rule. `grep` it to answer "which
  rule did that?":
  ```bash
  grep '"before": "\$1.2M"' data/jobs/*/transforms.jsonl
  ```
- `unknowns.jsonl` — terms with no lexicon entry, with counts and an example
  sentence. This is the input to `lexicon suggest`.

### Comparing backends

```bash
readcast compare <url> --backends mlx,elevenlabs --minutes 4
```

Prepares the text once, synthesizes the opening with each backend, and prints
real-time factor, wall clock, flagged chunks, and estimated cost for the *whole*
article. Use one full article, not a short sample — a short sample is where
every backend sounds fine.

## Voices

An autoregressive model samples a new speaker on every request, so a long
episode arrives read by a rotating cast — measured on one render, pitch swung
between 103 and 353 Hz across chunks. Conditioning each request on a reference
clip fixes it: 50 Hz of spread down to 14 Hz.

What grates is switching **inside** an article. Variety **between** articles is
worth having, so the default is a cast per episode: a narrator picked from a
pool, fixed for the whole piece, and a different one next time. The narrator
that has not read for longest goes next, so two neighbours never match.

```bash
readcast voices sample -n 4    # add voices to the pool
readcast voices show           # the pool, with pitch and who read what
readcast voices play all       # listen through them
readcast voices assign <job-id> 3   # pin a narrator to one article
```

Each queued episode carries its narrator in the web dashboard, with **▶** to
hear it and **↻** to roll a random different one — which plays the new voice
straight away, since the only way to judge it is by ear. While a sample plays
the button becomes a stop control, so a voice you have already made your mind up
about does not have to finish. Rolling picks at random
from the pool rather than stepping through it, so keep the pool large:
`readcast voices sample -n 18` pre-renders a selection, which takes about half a
minute per voice and cannot run while an episode is rendering. A job is cast when its text
becomes ready, so the narrator can be settled long before the render starts, and
it cannot be changed once synthesis is under way.

Within an episode there are three parts:

| Role | Reads |
| --- | --- |
| `main` | intro, headings, body |
| `quote` | blockquotes |
| `aside` | paragraphs that are entirely parenthetical |

`quote` and `aside` are chosen as the pool voices least like that episode's
narrator, so a quotation is audibly someone else. A narrator is fixed for one
episode, not for the feed.

Set `tts.voice_mode: fixed` in `config.yml` for one cast across everything;
`tts.voices` then names the three clips.

A job records its cast the first time it synthesizes, so a rerender or a resume
sounds the same. The chunk reuse cache keys on the voice, so changing one never
silently reuses the old audio.

## Quality controls

- **Chunking** splits on sentence boundaries only, 200–400 characters, never
  across a heading. Long inputs make speech models drop words; small chunks
  bound the damage and make a retry cheap.
- **Verification** transcribes each chunk with `mlx-whisper` and compares it to
  the input. Above 0.15 character error rate the chunk is re-synthesized with a
  new seed, twice. A chunk that still fails is kept, counted in
  `flagged_chunks`, and named in the episode description. Turn it off with
  `verify.enabled: false` when you want speed.

  Two details keep the check honest. Whisper writes numbers as numerals — it
  hears "thirty-eight percent" and transcribes "38%" — so both sides go through
  the same number handling before they are compared; without that, every number
  scores as an error and good audio gets re-rendered. And chunks under
  `verify.min_chars` (40) skip the check entirely: on a one-word heading a
  single mis-heard syllable scores 0.45, and whisper is unreliable on a
  one-second clip. Dropped words are a long-input problem.
- **Loudness** is normalized two-pass to −16 LUFS / −1.5 dBTP, the podcast
  convention. Without it the episode sits at a different volume from every other
  show in the queue.
- **Chapters** are written as ID3 `CHAP`/`CTOC` frames, one per heading, so a
  section can be skipped.

## Commands

```
readcast serve                            readcast rules test
readcast watch                            readcast lexicon suggest [--since 30d]
readcast show <id> [--chunks]             readcast extension-link <ext-id>
readcast add <url> [--backend B]          readcast compare <url> --backends A,B
readcast jobs [--state S]                 readcast feed rotate | rebuild | url
readcast rerender <id> [--from STAGE]     readcast bookmarklet
readcast preview <id> [--minutes N]       readcast init
readcast say <text> [--compare ...]
```

## HTTP

```
POST   /jobs                      auth: Bearer   -> 202 {"id": ...}
GET    /jobs?state=failed         auth: Bearer
GET    /jobs/{id}                 auth: Bearer   (job row plus stage timings)
POST   /jobs/{id}/rerender?from=preparing        auth: Bearer
DELETE /jobs/{id}                 auth: Bearer   (drops from feed, keeps files)
GET    /status                                   read-only HTML, last 50 jobs
GET    /f/{token}/feed.xml                       GET and HEAD
GET    /f/{token}/audio/{id}.mp3                 GET and HEAD, range requests
```

## Two lanes: text now, audio later

Preparation runs the moment a job is submitted, in its own lane. `spoken.txt` is
on disk seconds later, while the expensive half waits its turn. The spec's
"exactly one worker" rule is about the speech model's several gigabytes — text
preparation touches no model, so making it queue behind a two-hour render would
mean not seeing the text until long after submitting it.

```
submit ──► queued ──► [prep lane] ──► ready ──► [audio lane] ──► done
                      fetch/extract   text on   synthesize
                      /prepare        disk      /assemble
```

`ready` means the text is written and nothing expensive has happened yet:

```bash
readcast show <job-id>            # read it
readcast show <job-id> --chunks   # see the exact per-call strings
$EDITOR data/jobs/<job-id>/spoken.txt   # change it by hand if you like
readcast prep <job-id>            # or re-run the rules over it
```

Hand edits survive: the audio lane reads `spoken.txt` from disk, so whatever is
in that file is what gets spoken. Re-running `prep` overwrites it from the rules.

**new: hold / new: auto** in the dashboard header decides what happens to an
article once its text is ready — render it straight away, or stop and wait so
the text can be read and the narrator chosen first. It is a runtime switch
stored in the database, so it survives a restart and needs no file edit:

```bash
readcast hold-new          # show the current position
readcast hold-new on       # new articles wait for you
readcast hold-new off      # new articles render as soon as they are ready
```

`pipeline.hold_for_review` in `config.yml` sets the starting position; the
switch overrides it from then on.

Every queue row has **✕** to take it out, and the running job has **stop**.
Stopping is not failing: the chunks already made are kept, so releasing the job
again carries on from where it stopped rather than starting over. A cancelled
job never reaches the feed.

```bash
readcast cancel <id>            # stop a render, or drop a waiting job
readcast remove <id> [--files]  # remove it entirely
```

To decide per article rather than letting the queue drain:

```bash
readcast hold <job-id>      # keep it out of the audio lane
readcast release <job-id>   # send it through
```

Set `pipeline.hold_for_review: true` in `config.yml` to hold every job by
default.

### Long renders survive restarts

Each finished chunk is written with a sidecar holding its exact text. A job that
resumes reuses every chunk whose text still matches, so an interrupted
two-hour render picks up where it left off instead of starting over. Change the
text and only the changed chunks are re-synthesized.

## PDFs

A PDF describes marks on a page, not a document, so everything an HTML
extractor gets for free has to be recovered. Submit the URL and readcast fetches
the document itself:

```bash
readcast add https://arxiv.org/pdf/1706.03762
```

Four things ruin a narrated paper, and all four are handled:

| | |
| --- | --- |
| a word broken across a line | rejoined — `transduc-/tion` is one word, `well-/known` is two, decided against the bundled wordlist rather than by a rule about capitals |
| page numbers and running heads | dropped: lines repeated at the top or bottom of several pages are furniture |
| front matter | licence grants, author lists, affiliations and emails, cut back to the abstract |
| the reference list | a quarter of a typical paper, and unlistenable |
| ligatures | `ﬁ` and `ﬃ` arrive as single glyphs; resolved before de-hyphenation, since otherwise the wordlist cannot recognise `speciﬁc` |
| the title | many PDFs carry no `/Title`, and one that disagrees with the first page loses to it — but metadata is never discarded for nothing. A title that ends on a preposition is joined with the line below; one that does not is left alone, so the author is not swallowed |

Section numbers become chapter marks, so `3.2 Attention` is a chapter called
*Attention*. Everything is tunable per source — see
`rules/domains/arxiv.org.yml` for the full set, including which headings count
as back matter.

**Do not use the extension for a PDF.** Chrome renders one in its own viewer, so
what the extension captures is an empty shell. readcast notices, discards it and
fetches the document itself, but submitting the URL directly is one step shorter.

A scanned PDF has no text layer, and readcast says so rather than producing an
empty episode.

## Watching it work

```bash
readcast ui             # prints the browser dashboard URL, token filled in
readcast watch          # the same data in the terminal; --once for a snapshot
```

The web dashboard at `/ui` adds the two things that want a mouse: **drag the
queue** (or use the per-row ↑/↓ buttons) to change what renders next, and
**click any title** to read and edit its `spoken.txt` in place. The page is
self-contained, asks the browser for the API token once, and keeps it in
`localStorage`. Editing is refused while the audio lane holds a job — hold it
first.

Service dots, the job running right now with a chunk progress bar and an ETA,
the queue in run order, and recent jobs with their real-time factors. It only
reads the database and the chunk files, so it is safe to run alongside the
worker or in several terminals at once.

Progress comes from the worker's own report rather than from counting chunk
files, because a rerender overwrites those in place and the count would sit
still while the render was in fact moving.

The ETA is seeded with a measured seconds-per-chunk for this machine, then
pulled toward a decaying average of the chunks this run has actually finished,
and shown as both a duration and a clock time (`eta 15m26s · done 22:29`). Two
details keep it honest: reused chunks are hardlinks that keep the mtime of the
render that made them, so only files written since the current run's plan count
toward the rate; and a single stalled chunk is clamped rather than allowed to
redefine the estimate. The learned rate is stored and refined after every job.

In the editor, the buttons shown depend on what the job can actually do:
`Hold` and `Release to audio` appear only for a job waiting in the queue, and a
finished episode instead gets `Re-render`, which names the machine time it will
cost and asks before spending it. Release only clears the hold — it never starts
a render.

Keys: **1-9** opens that row's text in your pager, **c** toggles between
`spoken.txt` and the per-chunk breakdown, **q** quits. The same views without
the dashboard:

```bash
readcast show <job-id>            # spoken.txt: what the engine is given
readcast show <job-id> --chunks   # what each individual synthesis call receives
readcast show <job-id> --raw      # plain text, pipe it anywhere
```

A queued job has no text yet — `spoken.txt` only exists once the preparing
stage has run — so the view says so rather than showing an empty panel.

## Why it is slow, concretely

Breeze TTS 2 is an autoregressive LLM, not a vocoder. Its config tells the
story:

- a **Qwen3 backbone**, 28 layers at hidden size 2048 (~1.7B parameters),
- a **12-layer depth decoder** that emits **16 codebook tokens per audio frame**,
- a codec running at **25 frames per second** (24 kHz ÷ 960).

So one second of audio costs 25 backbone passes plus 400 depth-decoder passes —
**425 sequential forward passes per second of speech**, each depending on the
one before it, at batch size 1, in bf16 (7 GB of weights, memory-bandwidth
bound). A 6:31 episode is roughly 166,000 forward passes. Measured steady state
on an M-series Mac: **RTF 0.32**, about three minutes of compute per minute of
audio.

Things that do *not* explain it, measured rather than assumed:

- **Verification is nearly free.** Whisper transcribes a 24-second chunk in
  0.4 s — about 1% on top of synthesis. What cost real time was *retries*, each
  one a full re-synthesis. Fixing the false positives (§ numerals and
  `min_chars`) took one article from 9 retries to 2, and its RTF from 0.18 to
  0.32 — 35 minutes down to 20.
- **Concurrency does not help; it corrupts.** Three overlapping requests to
  `mlx-audio` returned 73 seconds of audio for text that takes 22 seconds
  sequentially: the model babbles when requests interleave. The synthesis stage
  stays sequential on purpose.

If you want it faster, in order of cost: leave `verify.enabled: true` (it is
cheap and catches real dropouts), switch `tts.backend` to `kokoro` for a
non-autoregressive model that runs many times faster with less prosody, or
accept the render time and queue long pieces overnight. Quantized Breeze weights
would help — they are also what the model card warns changes the audio quality
you are running a local model to get.

## Tests

```bash
uv run pytest                  # the whole suite
uv run pytest -m "not slow"    # skip the 6000-word end-to-end run
readcast rules test            # rule cases only; run this in rules/ CI
```

The suite covers the acceptance list: a 6000-word article to one MP3 with
correct duration metadata, no `[` / `Fig.` / `$` / `%` / `http` in the fixture
`spoken.txt` files, byte-identical `feed rebuild`, one job at a time, failed
jobs kept out of the feed, rerender keeping the guid while changing `?v=`,
enclosure length matching bytes on disk, range requests returning 206, and a
rotated token returning 404.

Speech-dependent tests use a built-in `test` backend that generates a tone, so
the suite runs with no model and no network.

## Known edges

- Dropping a URL can leave a dangling preposition ("published at and the
  methodology"). Set `urls.action: say_domain` if that bothers you more than
  hearing domains does.
- A slash next to a frozen span survives the generic slash rule
  (`$30/mTok` → `thirty dollars/mTok`). That is the frozen-span rule working as
  intended; add a lexicon entry for the term.
- `trafilatura`'s precision mode drops every heading on some sites. readcast
  falls back to the recall pass when precision finds no headings at all, and a
  domain file can set `extract.favor_precision: false` outright.

## License

The project code is MIT — see [LICENSE](LICENSE).

The Breeze TTS 2 **weights** carry a research and non-commercial license; the
inference code is Apache 2.0. A personal feed is fine. Anything client-facing or
revenue-generating needs a separate license from the vendor, or a different
backend. Every job row records the backend and model that produced it, so the
provenance of any episode stays clear.
