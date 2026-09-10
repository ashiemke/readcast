"""One voice per role, and the plumbing that keeps it that way."""

from __future__ import annotations

import pytest

from readcast.chunk import plan_chunks
from readcast.prepare.loader import load_rules
from readcast.prepare.pipeline import prepare_text
from readcast.synth.runner import synthesize_chunks
from readcast.synth.testing import TestBackend
from readcast.voices import (
    REFERENCE_TEXT, Voice, choose_distinct, estimate_pitch, load_voices,
    silence_reference,
)
from tests.conftest import REPO


def test_roles_map_chunk_kinds_to_voices(cfg):
    voices = load_voices(cfg)
    assert voices["body"].role == "main"
    assert voices["intro"].role == "main"
    assert voices["heading"].role == "main"
    assert voices["quote"].role == "quote"
    assert voices["aside"].role == "aside"


def test_a_missing_clip_is_reported_not_faked(cfg):
    voices = load_voices(cfg)
    assert not voices["body"].available
    assert voices["body"].payload() == {}


def test_a_recorded_clip_becomes_a_reference(cfg, tmp_path):
    clip = silence_reference(tmp_path / "main.wav")
    cfg.raw["tts"]["voices"] = {"main": str(clip)}
    voice = load_voices(cfg)["body"]
    assert voice.available
    payload = voice.payload()
    assert payload["ref_audio"] == str(clip.resolve())
    assert payload["ref_text"] == REFERENCE_TEXT


def test_changing_the_clip_changes_the_cache_identity(tmp_path):
    clip = silence_reference(tmp_path / "v.wav")
    voice = Voice(role="main", audio=clip)
    before = voice.cache_id
    silence_reference(clip, seconds=1.4)          # re-recorded
    assert voice.cache_id != before
    assert Voice(role="main").cache_id == "main:none"


def test_each_kind_is_synthesized_with_its_own_voice(cfg, tmp_path):
    voices = {
        "body": Voice(role="main", audio=silence_reference(tmp_path / "main.wav")),
        "quote": Voice(role="quote", audio=silence_reference(tmp_path / "quote.wav")),
    }
    from readcast.chunk import Chunk

    chunks = [
        Chunk(index=0, text="Narration for the body of the piece.", kind="body"),
        Chunk(index=1, text="A line someone else wrote entirely.", kind="quote"),
    ]
    backend = TestBackend({"chars_per_second": 400})
    synthesize_chunks(chunks, backend, tmp_path / "out", voices=voices)

    assert backend.calls[0]["reference"]["ref_audio"].endswith("main.wav")
    assert backend.calls[1]["reference"]["ref_audio"].endswith("quote.wav")


def test_the_same_words_in_a_different_voice_are_not_reused(cfg, tmp_path):
    """A quote is not interchangeable with narration, even word for word."""
    from readcast.chunk import Chunk

    line = "The very same sentence, read twice over."
    main = {"body": Voice(role="main", audio=silence_reference(tmp_path / "m.wav"))}
    quote = {"quote": Voice(role="quote", audio=silence_reference(tmp_path / "q.wav"))}

    out = tmp_path / "out"
    synthesize_chunks([Chunk(index=0, text=line, kind="body")],
                      TestBackend({"chars_per_second": 400}), out, voices=main)
    backend = TestBackend({"chars_per_second": 400})
    result = synthesize_chunks([Chunk(index=0, text=line, kind="quote")],
                               backend, out, voices=quote)
    assert result.reused == 0
    assert len(backend.calls) == 1


def test_blockquotes_and_asides_become_their_own_kinds():
    ruleset = load_rules(REPO / "rules")
    spoken = prepare_text(
        "Narration here.\n\n> Someone else wrote this line.\n> And this one.\n\n"
        "(A parenthetical aside.)\n\nMore narration.",
        ruleset, collect_unknowns=False,
    ).spoken
    kinds = {c.kind: c.text for c in plan_chunks(spoken)}
    assert "quote" in kinds and "aside" in kinds
    assert kinds["quote"].startswith("Someone else wrote this line.")
    assert kinds["aside"] == "A parenthetical aside."
    # The markers are for the operator; they are never spoken.
    assert ">" not in " ".join(c.text for c in plan_chunks(spoken))


def test_a_comparison_is_still_read_as_a_comparison():
    """The blockquote marker must not eat the greater-than symbol."""
    ruleset = load_rules(REPO / "rules")
    spoken = prepare_text("Is 5 > 3 true?", ruleset, collect_unknowns=False).spoken
    assert "greater than" in spoken
    assert not spoken.startswith(">")


def test_pitch_estimate_tracks_a_known_tone(tmp_path):
    import math
    import struct
    import wave

    for hz in (110, 220):
        path = tmp_path / f"{hz}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
            step = 2 * math.pi * hz / 24000
            handle.writeframes(b"".join(
                struct.pack("<h", int(9000 * math.sin(step * i))) for i in range(24000)))
        assert estimate_pitch(path) == pytest.approx(hz, rel=0.05)

    assert estimate_pitch(tmp_path / "missing.wav") is None


def test_distinct_voices_are_chosen_by_spread(tmp_path):
    candidates = [(tmp_path / f"{i}.wav", hz)
                  for i, hz in enumerate([100, 102, 104, 180, 250])]
    picked = choose_distinct(candidates, 3)
    pitches = sorted(dict(candidates)[p] for p in picked)
    assert len(picked) == 3
    assert max(pitches) - min(pitches) >= 100, "the picks should not sound alike"


def test_mlx_uses_the_field_names_the_server_declares():
    """`instructions` and `seed` are silently ignored by mlx-audio."""
    from readcast.synth.openai_compat import MLXBackend, OpenAIBackend

    body = MLXBackend({"temperature": 0.6})._payload(
        "hello", "v", "read calmly", 7, {"ref_audio": "/a.wav", "ref_text": "t"}
    )
    assert body["instruct"] == "read calmly"
    assert "instructions" not in body
    assert "seed" not in body
    assert body["ref_audio"] == "/a.wav"
    assert body["temperature"] == 0.6

    # OpenAI keeps its own spelling and does take a seed.
    other = OpenAIBackend({}).  _payload("hello", "v", "read calmly", 7)
    assert other["instructions"] == "read calmly"
    assert other["seed"] == 7


# -- a cast per episode -----------------------------------------------------


def _pool(cfg, pitches=(100, 140, 200, 260)):
    from readcast.voices import pool_dir

    directory = pool_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for i, hz in enumerate(pitches):
        path = directory / f"{i:02d}.wav"
        _tone(path, hz)
        made.append(path)
    return made


def _tone(path, hz, seconds=1.0, rate=24000):
    import math
    import struct
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(rate)
        step = 2 * math.pi * hz / rate
        handle.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(step * i)))
            for i in range(int(seconds * rate))))
    return path


def test_the_narrator_is_stable_within_one_episode(cfg, db):
    from readcast.voices import voices_for_job

    _pool(cfg)
    job_id = db.create_job(id="2026-09-10-aaaaaa", url="https://example.org/a")
    job = db.get_job(job_id)

    first = voices_for_job(cfg, db, job)
    again = voices_for_job(cfg, db, db.get_job(job_id))
    assert first["body"].audio == again["body"].audio
    # Every narrated kind shares the narrator.
    assert first["body"].audio == first["intro"].audio == first["heading"].audio
    # The cast is recorded, so a rerender sounds the same.
    assert db.get_job(job_id)["voice_main"] == first["body"].audio.name


def test_consecutive_episodes_get_different_narrators(cfg, db):
    from readcast.voices import voices_for_job

    _pool(cfg)
    narrators = []
    for i in range(4):
        job_id = db.create_job(id=f"2026-09-10-job{i:03d}", url=f"https://example.org/{i}")
        narrators.append(voices_for_job(cfg, db, db.get_job(job_id))["body"].audio.name)
    assert len(set(narrators)) == 4, f"a narrator repeated too soon: {narrators}"


def test_quotes_and_asides_differ_from_the_narrator(cfg, db):
    from readcast.voices import voices_for_job

    _pool(cfg)
    job_id = db.create_job(id="2026-09-10-bbbbbb", url="https://example.org/b")
    voices = voices_for_job(cfg, db, db.get_job(job_id))
    assert voices["quote"].audio != voices["body"].audio
    assert voices["aside"].audio != voices["body"].audio


def test_an_empty_pool_falls_back_without_crashing(cfg, db):
    from readcast.voices import voices_for_job

    job_id = db.create_job(id="2026-09-10-cccccc", url="https://example.org/c")
    voices = voices_for_job(cfg, db, db.get_job(job_id))
    assert set(voices) >= {"body", "quote", "aside"}
    assert not voices["body"].available          # nothing recorded, and it says so


def test_fixed_mode_still_uses_the_three_configured_voices(cfg, db, tmp_path):
    from readcast.voices import silence_reference, voices_for_job

    cfg.raw["tts"]["voice_mode"] = "fixed"
    clip = silence_reference(tmp_path / "main.wav")
    cfg.raw["tts"]["voices"] = {"main": str(clip)}
    _pool(cfg)
    job_id = db.create_job(id="2026-09-10-dddddd", url="https://example.org/d")
    voices = voices_for_job(cfg, db, db.get_job(job_id))
    assert voices["body"].audio == clip
    assert db.get_job(job_id)["voice_main"] is None   # no per-episode cast recorded


def test_a_pinned_narrator_is_kept(cfg, db):
    from readcast.voices import voices_for_job

    pool = _pool(cfg)
    job_id = db.create_job(id="2026-09-10-eeeeee", url="https://example.org/e")
    db.update_job(job_id, voice_main=pool[3].name, voice_quote=pool[0].name,
                  voice_aside=pool[1].name)
    voices = voices_for_job(cfg, db, db.get_job(job_id))
    assert voices["body"].audio.name == pool[3].name
    assert voices["quote"].audio.name == pool[0].name


def test_rerolling_is_random_rather_than_a_two_way_toggle(cfg, db):
    """An ordered pick walks between the same two voices; the button should not."""
    from readcast.voices import assign_cast, reroll_cast

    _pool(cfg, pitches=(100, 130, 160, 190, 220, 250, 280, 310))
    job_id = db.create_job(id="2026-09-10-rrrrrr", url="https://example.org/r")
    assign_cast(db, cfg, db.get_job(job_id))

    seen = set()
    for _ in range(30):
        cast = reroll_cast(db, cfg, db.get_job(job_id))
        seen.add(cast["main"].name)
    assert len(seen) >= 4, f"re-roll only ever reached {seen}"


def test_a_reroll_never_returns_the_current_narrator(cfg, db):
    from readcast.voices import assign_cast, reroll_cast

    _pool(cfg, pitches=(100, 200))
    job_id = db.create_job(id="2026-09-10-ssssss", url="https://example.org/s")
    assign_cast(db, cfg, db.get_job(job_id))
    for _ in range(6):
        before = db.get_job(job_id)["voice_main"]
        after = reroll_cast(db, cfg, db.get_job(job_id))["main"].name
        assert after != before


def test_automatic_casting_stays_ordered_so_neighbours_differ(cfg, db):
    """Only the button is random; consecutive episodes must not collide."""
    from readcast.voices import voices_for_job

    _pool(cfg, pitches=(100, 150, 200, 250))
    picks = []
    for i in range(4):
        job_id = db.create_job(id=f"2026-09-10-ord{i:03d}", url=f"https://example.org/{i}")
        picks.append(voices_for_job(cfg, db, db.get_job(job_id))["body"].audio.name)
    assert len(set(picks)) == 4


def test_a_clip_carries_its_own_transcript(cfg, tmp_path):
    """Changing the default sentence must not mislabel older recordings."""
    from readcast.voices import REFERENCE_TEXT, Voice, silence_reference

    clip = silence_reference(tmp_path / "old.wav")
    clip.with_suffix(".txt").write_text("The sentence this clip actually says.")
    voice = Voice(role="main", audio=clip, text=REFERENCE_TEXT)
    assert voice.resolved_text() == "The sentence this clip actually says."
    assert voice.payload()["ref_text"] == "The sentence this clip actually says."

    # With no sidecar it falls back to the configured text.
    bare = silence_reference(tmp_path / "bare.wav")
    assert Voice(role="main", audio=bare, text="configured").resolved_text() == "configured"


def test_the_reference_sentence_makes_no_claim_about_the_feed():
    from readcast.voices import REFERENCE_TEXT

    assert "episode" in REFERENCE_TEXT
    assert "feed" not in REFERENCE_TEXT
    assert "from one episode to the next" not in REFERENCE_TEXT


def test_pitch_is_measured_once_and_remembered(cfg, tmp_path, monkeypatch):
    """Re-measuring the pool on every re-roll made the button take seconds."""
    import readcast.voices as voices_mod

    clip = _tone(tmp_path / "00.wav", 150)
    calls = []
    real = voices_mod.estimate_pitch

    def counted(path, *a, **k):
        calls.append(path)
        return real(path, *a, **k)

    monkeypatch.setattr(voices_mod, "estimate_pitch", counted)

    first = voices_mod.pitch_of(clip)
    assert first == pytest.approx(150, rel=0.05)
    assert len(calls) == 1
    assert (tmp_path / voices_mod.PITCH_CACHE).is_file()

    for _ in range(5):
        assert voices_mod.pitch_of(clip) == first
    assert len(calls) == 1, "the pitch was measured again"


def test_a_re_recorded_clip_is_measured_again(cfg, tmp_path):
    """The cache is keyed by size and mtime, not by name."""
    import os

    import readcast.voices as voices_mod

    clip = _tone(tmp_path / "00.wav", 120)
    assert voices_mod.pitch_of(clip) == pytest.approx(120, rel=0.05)

    _tone(clip, 240)                                  # same name, new recording
    os.utime(clip, (clip.stat().st_mtime + 5, clip.stat().st_mtime + 5))
    assert voices_mod.pitch_of(clip) == pytest.approx(240, rel=0.05)


def test_casting_reads_cached_pitches(cfg, tmp_path, monkeypatch):
    import readcast.voices as voices_mod

    pool = _pool(cfg, pitches=(100, 150, 200, 250))
    voices_mod.cast_around(pool, pool[0])              # fills the cache

    monkeypatch.setattr(
        voices_mod, "estimate_pitch",
        lambda *a, **k: pytest.fail("re-roll should not re-measure the pool"),
    )
    cast = voices_mod.cast_around(pool, pool[1])
    assert cast["main"] == pool[1]
    assert cast["quote"] != cast["main"]


def test_a_corrupt_cache_is_ignored_rather_than_fatal(cfg, tmp_path):
    import readcast.voices as voices_mod

    clip = _tone(tmp_path / "00.wav", 180)
    (tmp_path / voices_mod.PITCH_CACHE).write_text("{not json")
    assert voices_mod.pitch_of(clip) == pytest.approx(180, rel=0.05)


# -- backends whose speakers are names, not recordings ----------------------


def test_a_named_voice_backend_gets_a_name_per_episode(cfg, db):
    from readcast.voices import voices_for_job

    cfg.raw["tts"]["backends"]["kokoro"] = {"voices": ["am_michael", "bf_emma", "bm_george"]}
    job_id = db.create_job(id="2026-09-10-nnnnnn", url="https://example.org/n")
    voices = voices_for_job(cfg, db, db.get_job(job_id), backend="kokoro")

    assert voices["body"].name in ("am_michael", "bf_emma", "bm_george")
    assert voices["quote"].name != voices["body"].name
    # A name travels in the voice field, not as a reference clip.
    assert voices["body"].payload() == {}
    assert db.get_job(job_id)["voice_main"] == voices["body"].name


def test_named_voices_still_key_the_reuse_cache(cfg):
    from readcast.voices import Voice

    assert Voice(role="main", name="am_michael").cache_id != \
        Voice(role="main", name="bf_emma").cache_id


def test_the_narrator_reaches_a_named_voice_backend(cfg, tmp_path):
    from readcast.chunk import Chunk
    from readcast.voices import Voice

    backend = TestBackend({"chars_per_second": 400})
    backend.supports_reference = False
    voices = {"body": Voice(role="main", name="bm_george")}
    synthesize_chunks(
        [Chunk(index=0, text="A line of narration here.", kind="body")],
        backend, tmp_path, voice="default", voices=voices,
    )
    assert backend.calls[0]["voice"] == "bm_george"
    assert backend.calls[0]["reference"] is None


def test_a_reference_clip_is_withheld_from_a_backend_that_ignores_it(cfg, tmp_path):
    from readcast.chunk import Chunk
    from readcast.voices import Voice, silence_reference

    backend = TestBackend({"chars_per_second": 400})
    backend.supports_reference = False
    voices = {"body": Voice(role="main", audio=silence_reference(tmp_path / "m.wav"))}
    synthesize_chunks(
        [Chunk(index=0, text="A line of narration here.", kind="body")],
        backend, tmp_path / "out", voices=voices,
    )
    assert backend.calls[0]["reference"] is None


def test_rerolling_a_named_voice(cfg, db):
    from readcast.voices import reroll_named, voices_for_job

    names = ["am_michael", "bf_emma", "bm_george", "af_heart", "am_puck"]
    cfg.raw["tts"]["backends"]["kokoro"] = {"voices": names}
    job_id = db.create_job(id="2026-09-10-pppppp", url="https://example.org/p")
    voices_for_job(cfg, db, db.get_job(job_id), backend="kokoro")

    seen = set()
    for _ in range(20):
        before = db.get_job(job_id)["voice_main"]
        cast = reroll_named(db, cfg, db.get_job(job_id), "kokoro")
        assert cast["main"] != before
        seen.add(cast["main"])
    assert len(seen) >= 3

    pinned = reroll_named(db, cfg, db.get_job(job_id), "kokoro", name="af_heart")
    assert pinned["main"] == "af_heart"
    assert reroll_named(db, cfg, db.get_job(job_id), "kokoro", name="nope") is None


def test_kokoro_declares_what_it_can_and_cannot_do():
    from readcast.synth import get_backend

    kokoro = get_backend("kokoro", {})
    assert kokoro.supports_reference is False   # named packs, not clips
    assert kokoro.supports_seed is False
    assert get_backend("mlx", {}).supports_reference is True


def test_a_bullet_never_reaches_the_engine():
    """A lone bullet killed a two-hour render: the phonemizer produced nothing."""
    from readcast.chunk import plan_chunks
    from readcast.prepare.loader import load_rules
    from readcast.prepare.pipeline import prepare_text

    spoken = prepare_text(
        "Narration.\n\n• first point\n• second point\n\nMore narration.",
        load_rules(REPO / "rules"), collect_unknowns=False,
    ).spoken
    assert "•" not in spoken

    # And nothing unspeakable survives chunking, whatever the source did.
    chunks = plan_chunks("A real sentence here.\n\n•\n\n— \n\nAnother sentence.")
    assert all(any(c.isalnum() for c in chunk.text) for chunk in chunks)
    assert len(chunks) == 2


def test_an_unspeakable_chunk_is_skipped_not_fatal(tmp_path):
    """Belt and braces: one bad chunk must never cost the whole episode."""
    from readcast.chunk import Chunk
    from readcast.synth.runner import synthesize_chunks

    class Fussy(TestBackend):
        def synth(self, text, **kwargs):
            if not any(c.isalnum() for c in text):
                raise ConnectionError("peer closed connection")
            return super().synth(text, **kwargs)

    backend = Fussy({"chars_per_second": 400})
    result = synthesize_chunks(
        [Chunk(index=0, text="A real line of narration.", kind="body"),
         Chunk(index=1, text="•", kind="body"),
         Chunk(index=2, text="Another real line.", kind="body")],
        backend, tmp_path,
    )
    assert result.skipped == 1
    assert len(result.chunks) == 2
    assert all("•" not in c["text"] for c in backend.calls)
