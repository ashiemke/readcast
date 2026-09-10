"""Chunk verification: the check that catches silent word dropping."""

from __future__ import annotations

import pytest

from readcast.synth.runner import synthesize_chunks
from readcast.chunk import Chunk
from readcast.synth.testing import TestBackend
from readcast.synth.verify import character_error_rate, edit_distance, normalize


def test_normalize_ignores_case_and_punctuation_and_spells_numbers():
    assert normalize("The  BUDGET, was $3M!") == "the budget was three million dollars"
    assert normalize("Hello,   THERE!") == "hello there"


def test_edit_distance():
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("same", "same") == 0


def test_numerals_in_the_transcript_are_not_scored_as_errors():
    """Whisper writes "38%" for audio that said "thirty-eight percent"."""
    reference = "Cooling now accounts for thirty-eight percent of the operating budget."
    transcript = "Cooling now accounts for 38% of the operating budget."
    assert character_error_rate(reference, transcript) == 0.0

    # Money, years and ordinals normalize the same way.
    assert character_error_rate("It cost one point two million dollars.", "It cost $1.2M.") == 0.0
    assert character_error_rate("It shipped in nineteen eighty-four.", "It shipped in 1984.") == 0.0
    assert character_error_rate("The third attempt.", "The 3rd attempt.") == 0.0

    # A genuinely dropped phrase still scores as one.
    dropped = character_error_rate(reference, "Cooling now accounts of the operating budget.")
    assert dropped > 0.15


def test_cer_is_zero_for_a_perfect_transcript():
    assert character_error_rate("Hello there.", "hello there") == 0.0


def test_cer_rises_when_words_are_dropped():
    reference = "the quick brown fox jumps over the lazy dog"
    assert character_error_rate(reference, reference) == 0.0
    dropped = character_error_rate(reference, "the quick fox jumps over the lazy dog")
    assert dropped > 0.1


def test_a_failing_chunk_is_retried_then_flagged(tmp_path, monkeypatch):
    import readcast.synth.runner as runner

    class _Bad:
        cer = 0.9
        transcript = "nothing like it"
        ok = False
        available = True

    def always_wrong(path, text, *, model, max_cer):
        return _Bad()

    monkeypatch.setattr(runner, "verify_chunk", always_wrong)
    chunks = [
        Chunk(
            index=0,
            text="A sentence that the engine mangles, long enough to be worth checking.",
            kind="body",
        )
    ]
    result = synthesize_chunks(
        chunks, TestBackend({"chars_per_second": 400}), tmp_path,
        verify={"enabled": True, "max_cer": 0.15, "retries": 2, "min_chars": 40},
    )
    assert result.flagged_chunks == 1
    assert result.chunks[0].attempts == 3  # first try plus two retries
    assert result.chunks[0].flagged
    assert result.chunks[0].path.is_file()


def test_verification_is_skipped_when_whisper_is_absent(tmp_path, monkeypatch):
    import readcast.synth.verify as verify_mod

    def no_whisper():
        raise ImportError("mlx_whisper")

    monkeypatch.setattr(verify_mod, "_whisper", no_whisper)
    outcome = verify_mod.verify_chunk(tmp_path / "missing.wav", "text", model="m")
    assert outcome.ok
    assert not outcome.available


def test_rtf_is_recorded(tmp_path):
    chunks = [Chunk(index=i, text="A sentence of ordinary length here.", kind="body")
              for i in range(3)]
    result = synthesize_chunks(chunks, TestBackend({"chars_per_second": 400}), tmp_path)
    assert result.audio_seconds > 0
    assert result.synth_seconds > 0
    assert result.rtf == pytest.approx(result.audio_seconds / result.synth_seconds)
    assert [c.path.name for c in result.chunks] == ["000.wav", "001.wav", "002.wav"]


def test_instructions_are_passed_per_chunk_kind(tmp_path):
    backend = TestBackend({"chars_per_second": 400})
    chunks = [
        Chunk(index=0, text="Title here.", kind="intro"),
        Chunk(index=1, text="A heading", kind="heading"),
        Chunk(index=2, text="Body text.", kind="body"),
    ]
    synthesize_chunks(
        chunks, backend, tmp_path,
        instructions={"intro": "announce", "heading": "slower", "body": "narrate"},
    )
    assert [c["instruction"] for c in backend.calls] == ["announce", "slower", "narrate"]


def test_short_chunks_skip_verification(tmp_path, monkeypatch):
    """A one-word heading scores a huge CER on one mis-heard syllable."""
    import readcast.synth.runner as runner

    calls = []

    class _Bad:
        cer, transcript, ok, available = 0.9, "wrong", False, True

    def spy(path, text, *, model, max_cer):
        calls.append(text)
        return _Bad()

    monkeypatch.setattr(runner, "verify_chunk", spy)
    chunks = [
        Chunk(index=0, text="Bookmarklet", kind="heading"),
        Chunk(index=1, text="A body sentence long enough to be worth checking properly.",
              kind="body"),
    ]
    result = synthesize_chunks(
        chunks, TestBackend({"chars_per_second": 400}), tmp_path,
        verify={"enabled": True, "max_cer": 0.15, "retries": 2, "min_chars": 40},
    )
    # The heading was never sent to whisper; the body was, and was flagged.
    assert calls and all("Bookmarklet" != c for c in calls)
    assert result.chunks[0].attempts == 1
    assert not result.chunks[0].flagged
    assert result.chunks[1].flagged


def test_finished_chunks_are_reused_after_an_interruption(tmp_path):
    """A 40-minute article is a two-hour render. A restart must not redo it."""
    from readcast.chunk import Chunk

    chunks = [
        Chunk(index=i, text=f"Sentence number {i} of the article, at some length.",
              kind="body")
        for i in range(4)
    ]
    backend = TestBackend({"chars_per_second": 400})
    first = synthesize_chunks(chunks, backend, tmp_path)
    assert first.reused == 0
    assert len(backend.calls) == 4

    # Same plan, fresh backend: everything on disk is reused.
    again = TestBackend({"chars_per_second": 400})
    second = synthesize_chunks(chunks, again, tmp_path)
    assert second.reused == 4
    assert again.calls == []
    assert second.audio_seconds == pytest.approx(first.audio_seconds, rel=0.01)


def test_changed_text_is_re_synthesized_not_reused(tmp_path):
    from readcast.chunk import Chunk

    original = [Chunk(index=0, text="The original sentence here.", kind="body")]
    synthesize_chunks(original, TestBackend({"chars_per_second": 400}), tmp_path)

    edited = [Chunk(index=0, text="The operator changed this line.", kind="body")]
    backend = TestBackend({"chars_per_second": 400})
    result = synthesize_chunks(edited, backend, tmp_path)
    assert result.reused == 0
    assert backend.calls[0]["text"] == "The operator changed this line."


def test_resume_can_be_turned_off(tmp_path):
    from readcast.chunk import Chunk

    chunks = [Chunk(index=0, text="A sentence of ordinary length here.", kind="body")]
    synthesize_chunks(chunks, TestBackend({"chars_per_second": 400}), tmp_path)
    backend = TestBackend({"chars_per_second": 400})
    result = synthesize_chunks(chunks, backend, tmp_path, resume=False)
    assert result.reused == 0
    assert len(backend.calls) == 1


def test_editing_one_paragraph_does_not_re_render_the_rest(tmp_path):
    """Reuse is keyed by text, so an edit costs only the chunks that changed."""
    from readcast.chunk import Chunk

    original = [
        Chunk(index=i, text=f"Paragraph number {i} of the article, at length.", kind="body")
        for i in range(5)
    ]
    synthesize_chunks(original, TestBackend({"chars_per_second": 400}), tmp_path)

    # Insert a new paragraph at the front: every later index shifts by one.
    edited = [Chunk(index=0, text="A brand new opening line goes here.", kind="body")]
    edited += [
        Chunk(index=i + 1, text=original[i].text, kind="body") for i in range(5)
    ]
    backend = TestBackend({"chars_per_second": 400})
    result = synthesize_chunks(edited, backend, tmp_path)

    assert result.reused == 5, "shifted chunks should have been reused"
    assert len(backend.calls) == 1
    assert backend.calls[0]["text"] == "A brand new opening line goes here."


def test_a_dropped_connection_is_retried_before_the_job_dies(tmp_path, monkeypatch):
    """A speech server can drop one request; a two-hour job should survive it."""
    import readcast.synth.runner as runner
    from readcast.chunk import Chunk

    monkeypatch.setattr(runner, "TRANSIENT_BACKOFF_S", 0.01)

    class Flaky(TestBackend):
        def __init__(self):
            super().__init__({"chars_per_second": 400})
            self.attempts = 0

        def synth(self, text, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise ConnectionError("peer closed connection")
            return super().synth(text, **kwargs)

    backend = Flaky()
    result = synthesize_chunks(
        [Chunk(index=0, text="A line that the server chokes on twice.", kind="body")],
        backend, tmp_path,
    )
    assert backend.attempts == 3
    assert result.chunks[0].path.is_file()


def test_a_backend_that_never_recovers_still_fails(tmp_path, monkeypatch):
    import readcast.synth.runner as runner
    from readcast.chunk import Chunk

    monkeypatch.setattr(runner, "TRANSIENT_BACKOFF_S", 0.01)

    class Dead(TestBackend):
        def synth(self, text, **kwargs):
            raise ConnectionError("speech server is gone")

    with pytest.raises(ConnectionError):
        synthesize_chunks(
            [Chunk(index=0, text="Anything at all.", kind="body")], Dead(), tmp_path
        )
