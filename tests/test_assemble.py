"""Pauses, loudness, chapters and the intro line."""

from __future__ import annotations

from mutagen.mp3 import MP3

from readcast.assemble import assemble, build_intro, plan_timeline
from readcast.audio import probe_duration
from readcast.chunk import Chunk
from readcast.synth.runner import synthesize_chunks
from readcast.synth.testing import TestBackend

TEMPLATE = "{title}. From {publication}. By {author}. Published {published_at}."


def test_intro_omits_missing_fields_without_saying_unknown():
    full = build_intro(TEMPLATE, {
        "title": "A Piece", "publication": "Example", "author": "R. Okafor",
        "published_at": "2026-08-25",
    })
    assert full == "A Piece. From Example. By R. Okafor. Published August 25, 2026."

    partial = build_intro(TEMPLATE, {"title": "A Piece", "publication": "Example"})
    assert partial == "A Piece. From Example."
    assert "unknown" not in partial.lower()
    assert "None" not in partial

    assert build_intro(TEMPLATE, {"title": "Only A Title"}) == "Only A Title."


def test_intro_does_not_double_a_period_after_an_abbreviated_publisher():
    """A publisher ending in "Inc." plus the template's own period reads as a stumble."""
    out = build_intro(TEMPLATE, {
        "title": "Bookmarklet", "publication": "Wikimedia Foundation, Inc.",
        "author": "Contributors", "published_at": "2002-09-06",
    })
    assert ".." not in out
    assert "From Wikimedia Foundation, Inc. By Contributors." in out


def _audio(tmp_path, chunks):
    return synthesize_chunks(chunks, TestBackend({"chars_per_second": 400}), tmp_path)


def test_pauses_are_inserted_around_headings_and_paragraphs(tmp_path):
    chunks = [
        Chunk(index=0, text="Intro line.", kind="intro"),
        Chunk(index=1, text="Body one.", kind="body", ends_paragraph=True),
        Chunk(index=2, text="A heading", kind="heading"),
        Chunk(index=3, text="Body two.", kind="body", ends_paragraph=True),
    ]
    result = _audio(tmp_path, chunks)
    parts, chapters = plan_timeline(
        result.chunks,
        {"pause_paragraph_ms": 500, "pause_heading_ms": 1200, "pause_after_intro_ms": 1000},
        tmp_path,
    )
    names = [p.name for p in parts]
    assert names[0] == "000.wav"
    assert names[1] == "silence-1000.wav"      # after the intro
    assert "silence-1200.wav" in names          # before the heading
    assert names.index("silence-1200.wav") < names.index("002.wav")
    # The final chunk gets no trailing pause.
    assert names[-1] == "003.wav"
    assert [c.title for c in chapters] == ["A heading"]
    assert chapters[0].start_ms > 0


def test_assembled_episode_has_chapters_tags_and_expected_length(tmp_path):
    chunks = [
        Chunk(index=0, text="The title. From Example.", kind="intro"),
        Chunk(index=1, text="First body paragraph here.", kind="body", ends_paragraph=True),
        Chunk(index=2, text="Section one", kind="heading"),
        Chunk(index=3, text="More body text follows.", kind="body", ends_paragraph=True),
        Chunk(index=4, text="Section two", kind="heading"),
        Chunk(index=5, text="Closing body text.", kind="body", ends_paragraph=True),
    ]
    result = _audio(tmp_path / "chunks", chunks)
    speech = sum(c.duration_s for c in result.chunks)

    built = assemble(
        result.chunks,
        tmp_path / "episode.mp3",
        meta={
            "title": "The title", "author": "A Writer", "publication": "Example",
            "published_at": "2026-08-25", "url": "https://example.org/piece",
        },
        audio_cfg={"pause_paragraph_ms": 500, "pause_heading_ms": 1200,
                   "pause_after_intro_ms": 1000, "bitrate_kbps": 48},
    )

    assert built.path.is_file()
    assert built.bytes == built.path.stat().st_size
    assert abs(built.duration_s - probe_duration(built.path)) < 0.1
    # Speech plus the pauses we asked for: 1000 + 500 + 1200 + 500 + 1200 = 4.4s
    assert built.duration_s > speech + 4.0

    tags = MP3(built.path).tags
    assert tags["TIT2"].text[0] == "The title"
    assert tags["TPE1"].text[0] == "A Writer"
    assert tags["TALB"].text[0] == "readcast"
    assert str(tags["TDRC"].text[0]) == "2026-08-25"
    assert tags.getall("WOAS")[0].url == "https://example.org/piece"
    chapters = tags.getall("CHAP")
    assert [c.sub_frames["TIT2"].text[0] for c in chapters] == ["Section one", "Section two"]
    assert chapters[0].end_time == chapters[1].start_time
    assert chapters[-1].end_time == int(built.duration_s * 1000)
    assert tags.getall("CTOC")[0].child_element_ids == ["chp0", "chp1"]


def test_a_very_short_episode_still_assembles(tmp_path):
    """loudnorm cannot measure a fraction of a second; it must not fail the job."""
    chunks = [Chunk(index=0, text="Short.", kind="body")]
    result = _audio(tmp_path / "chunks", chunks)
    built = assemble(
        result.chunks, tmp_path / "short.mp3",
        meta={"title": "t"}, audio_cfg={"lufs": -16, "bitrate_kbps": 48},
    )
    assert built.path.is_file()
    assert built.duration_s > 0


def test_loudness_is_normalized(tmp_path):
    # Long enough for loudnorm to measure: the two-pass path needs a few seconds.
    chunks = [
        Chunk(index=i, text="A line of narration that runs on for a while here.", kind="body")
        for i in range(6)
    ]
    result = synthesize_chunks(chunks, TestBackend({"chars_per_second": 12}),
                               tmp_path / "chunks")
    built = assemble(
        result.chunks, tmp_path / "e.mp3",
        meta={"title": "t"}, audio_cfg={"lufs": -16, "bitrate_kbps": 48},
    )
    import subprocess

    proc = subprocess.run(
        ["ffmpeg", "-i", str(built.path), "-af", "loudnorm=I=-16:print_format=json",
         "-f", "null", "-"],
        capture_output=True, check=False,
    )
    text = proc.stderr.decode()
    measured = float(text[text.rfind("input_i") :].split('"')[2].strip(": ,\n"))
    assert -20 < measured < -12, f"episode measured {measured} LUFS"
