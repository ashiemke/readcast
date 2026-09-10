"""End to end, with a backend that needs no model.

Acceptance test 1: a 6000-word article completes and produces a single MP3
with correct duration metadata.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from mutagen.mp3 import MP3

from readcast.audio import probe_duration
from readcast.db import new_job_id
from readcast.worker import PrepWorker, RunOptions, Worker, run_job

WORDS = (
    "the system runs a rule over every paragraph before any audio exists which "
    "keeps the text editable and the pronunciation under version control "
).split()


def long_article_html(word_count: int = 6000) -> str:
    paragraphs = []
    words_per_paragraph = 60
    made = 0
    index = 0
    while made < word_count:
        chunk = [WORDS[(index + i) % len(WORDS)] for i in range(words_per_paragraph)]
        index += words_per_paragraph
        made += words_per_paragraph
        text = " ".join(chunk).capitalize() + "."
        if len(paragraphs) % 6 == 0:
            paragraphs.append(f"<h2>Section {len(paragraphs) // 6 + 1}</h2>")
        paragraphs.append(f"<p>{text}</p>")
    body = "\n".join(paragraphs)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>A long article</title>"
        "<meta property='og:site_name' content='Example'>"
        "<meta property='article:author' content='A Writer'>"
        "<meta property='article:published_time' content='2026-03-01'>"
        f"</head><body><article><h1>A long article</h1>{body}</article></body></html>"
    )


def submit(cfg, db, html: str, url: str = "https://example.org/long") -> str:
    job_id = db.create_job(id=new_job_id(), url=url, backend="test", client_html=1)
    directory = cfg.jobs_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raw.html").write_text(html)
    return job_id


@pytest.mark.slow
def test_six_thousand_word_article_produces_one_mp3(cfg, db):
    job_id = submit(cfg, db, long_article_html(6000))
    run_job(db, cfg, job_id)

    job = db.get_job(job_id)
    assert job["state"] == "done"
    assert job["word_count"] >= 5000

    directory = cfg.jobs_dir / job_id
    mp3s = list(directory.glob("*.mp3"))
    assert len(mp3s) == 1
    episode = mp3s[0]

    on_disk = probe_duration(episode)
    tagged = MP3(episode).info.length
    assert abs(tagged - on_disk) < 1.0
    assert abs(float(job["duration_s"]) - on_disk) < 1.0
    assert job["bytes"] == episode.stat().st_size

    tags = MP3(episode).tags
    assert tags["TIT2"].text[0] == "A long article"
    assert tags["TALB"].text[0] == "readcast"
    chapters = tags.getall("CHAP")
    assert len(chapters) >= 2
    assert tags.getall("CTOC")
    assert chapters[0].start_time < chapters[1].start_time

    # Every stage recorded its wall time, and the real-time factor is on the row.
    times = db.stage_times(job_id)
    assert {"extracting", "preparing", "synthesizing", "assembling"} <= set(times)
    assert job["rtf"] and job["rtf"] > 0


def test_a_failed_extraction_keeps_its_artifacts_and_fails_the_job(cfg, db):
    job_id = submit(cfg, db, "<html><body><p>too short</p></body></html>")
    with pytest.raises(Exception):
        run_job(db, cfg, job_id)
    job = db.get_job(job_id)
    assert job["state"] == "failed"
    assert job["stage_failed"] == "extracting"
    assert "too little text" in job["error"]
    # Artifacts already written stay on disk.
    assert (cfg.jobs_dir / job_id / "raw.html").is_file()


def test_rerender_from_preparing_reuses_the_fetch(cfg, db):
    job_id = submit(cfg, db, long_article_html(400))
    run_job(db, cfg, job_id)
    spoken = (cfg.jobs_dir / job_id / "spoken.txt").read_text()
    raw_mtime = (cfg.jobs_dir / job_id / "raw.html").stat().st_mtime

    # A rule change, then a rerender from preparing.
    lexicon = Path(cfg.rules_dir) / "lexicon.yml"
    lexicon.write_text(
        lexicon.read_text() + "\n  - match: paragraph\n    mode: respell\n    say: para graf\n"
    )
    run_job(db, cfg, job_id, RunOptions(from_stage="preparing"))

    assert (cfg.jobs_dir / job_id / "raw.html").stat().st_mtime == raw_mtime
    updated = (cfg.jobs_dir / job_id / "spoken.txt").read_text()
    assert updated != spoken
    assert "para graf" in updated
    assert db.get_job(job_id)["state"] == "done"


def test_preview_does_not_touch_the_feed(cfg, db):
    job_id = submit(cfg, db, long_article_html(1200))
    run_job(db, cfg, job_id)
    feed_before = (cfg.feed_dir / "feed.xml").read_bytes()
    episode_before = (cfg.jobs_dir / job_id / "episode.mp3").read_bytes()

    out_dir = cfg.jobs_dir / job_id / "preview"
    run_job(
        db, cfg, job_id,
        RunOptions(from_stage="preparing", preview_minutes=0.2, publish=False,
                   out_dir=out_dir, verify=False),
    )
    assert (out_dir / "episode.mp3").is_file()
    assert probe_duration(out_dir / "episode.mp3") < probe_duration(
        cfg.jobs_dir / job_id / "episode.mp3"
    )
    assert (cfg.feed_dir / "feed.xml").read_bytes() == feed_before
    assert (cfg.jobs_dir / job_id / "episode.mp3").read_bytes() == episode_before
    assert db.get_job(job_id)["state"] == "done"


def test_two_jobs_never_synthesize_at_the_same_time(cfg, db, monkeypatch):
    """Acceptance test 5: the audio lane is strictly one at a time.

    Text preparation now runs in its own lane, but that lane touches no model.
    The invariant that matters is that two jobs never hold the speech model.
    """
    import readcast.synth.testing as testing

    live = {"now": 0, "max": 0}
    lock = threading.Lock()
    original = testing.TestBackend.synth

    def counted(self, text, **kwargs):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        try:
            time.sleep(0.001)
            return original(self, text, **kwargs)
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(testing.TestBackend, "synth", counted)

    first = submit(cfg, db, long_article_html(300), "https://example.org/one")
    second = submit(cfg, db, long_article_html(300), "https://example.org/two")

    prep = PrepWorker(db, cfg, poll_seconds=0.05)
    worker = Worker(db, cfg, poll_seconds=0.05)
    assert prep.start()
    assert worker.start()
    assert not Worker(db, cfg).start()      # the audio lock refuses a second
    assert not PrepWorker(db, cfg).start()  # so does the prep lock
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            states = {db.get_job(j)["state"] for j in (first, second)}
            if states == {"done"}:
                break
            time.sleep(0.2)
    finally:
        worker.stop()
        prep.stop()

    assert db.get_job(first)["state"] == "done"
    assert db.get_job(second)["state"] == "done"
    assert live["max"] == 1, "two jobs synthesized at the same time"


def test_a_job_interrupted_by_a_crash_is_not_left_running(cfg, db):
    """A killed worker leaves a row mid-flight; the next one must not ignore it."""
    job_id = submit(cfg, db, long_article_html(200))
    db.update_job(job_id, state="synthesizing")

    worker = Worker(db, cfg, poll_seconds=0.05)
    assert worker.start()
    try:
        job = db.get_job(job_id)
        assert job["state"] == "failed"
        assert job["stage_failed"] == "synthesizing"
        assert "interrupted" in job["error"]
        # Artifacts are kept, so a rerender resumes from that stage.
        assert (cfg.jobs_dir / job_id / "raw.html").is_file()
    finally:
        worker.stop()


def test_the_queue_is_first_in_first_out(cfg, db):
    """Several submissions inside one second must still run in arrival order."""
    ids = [
        db.create_job(id=new_job_id(), url=f"https://example.org/{i}")
        for i in range(5)
    ]
    claimed = []
    while True:
        job = db.claim_next_queued()
        if job is None:
            break
        claimed.append(job["id"])
    assert claimed == ids


def test_a_pdf_is_read_rather_than_refused(cfg, db):
    """PDFs used to fail by design; they are read now."""
    from tests.pdfbuild import make_pdf

    job_id = db.create_job(id=new_job_id(), url="https://example.org/paper.pdf")
    directory = cfg.jobs_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    body = (
        "We examine the question carefully and report what we found, at "
        "sufficient length that the extractor has something real to work with. "
    )
    (directory / "raw.pdf").write_bytes(make_pdf(
        [
            ["A Paper With A Title", "Abstract"] + [body + str(i) for i in range(6)],
            ["1 Introduction"] + [body + f"intro {i}" for i in range(6)],
            ["2 Method"] + [body + f"method {i}" for i in range(6)],
        ],
        title="A Paper With A Title",
    ))

    run_job(db, cfg, job_id, RunOptions(from_stage="extracting", until_stage="preparing"))
    job = db.get_job(job_id)
    assert job["state"] == "ready"
    assert job["title"] == "A Paper With A Title"
    spoken = (directory / "spoken.txt").read_text()
    assert spoken.startswith("## Abstract")
    assert "Introduction" in spoken


def test_a_browser_captured_pdf_viewer_is_explained(cfg, db):
    """Chrome shows a PDF in its own viewer, so the captured page is a shell."""
    shell = (
        '<html><head><link href="chrome-extension://x/pdf_embedder.css">'
        "</head><body></body></html>"
    )
    job_id = submit(cfg, db, shell, "https://example.org/paper.pdf")
    with pytest.raises(Exception):
        run_job(db, cfg, job_id, RunOptions(from_stage="extracting"))
    error = db.get_job(job_id)["error"]
    assert "looks like a PDF" in error
    assert "without the browser extension" in error
