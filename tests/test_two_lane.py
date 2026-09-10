"""Preparation runs on submission; audio waits.

The point is to have the text on disk within seconds of submitting, so the
rules can be tuned before anything expensive starts.
"""

from __future__ import annotations

import time

from readcast.worker import PrepWorker, RunOptions, Worker, run_job
from tests.test_pipeline import long_article_html, submit


def _prep(cfg, db, job_id):
    run_job(db, cfg, job_id, RunOptions(from_stage="fetching", until_stage="preparing"))
    return db.get_job(job_id)


def test_preparation_stops_at_ready_with_the_text_on_disk(cfg, db):
    job_id = submit(cfg, db, long_article_html(600))
    job = _prep(cfg, db, job_id)

    assert job["state"] == "ready"
    assert job["word_count"] > 0
    directory = cfg.jobs_dir / job_id
    assert (directory / "spoken.txt").read_text().strip()
    assert (directory / "transforms.jsonl").is_file()
    assert (directory / "unknowns.jsonl").is_file()
    # Nothing expensive happened.
    assert not (directory / "episode.mp3").exists()
    assert not (directory / "chunks").exists()


def test_the_audio_lane_picks_up_a_ready_job(cfg, db):
    job_id = submit(cfg, db, long_article_html(400))
    _prep(cfg, db, job_id)
    claimed = db.claim_next_ready()
    assert claimed["id"] == job_id
    assert claimed["state"] == "synthesizing"


def test_a_held_job_is_never_claimed(cfg, db):
    job_id = submit(cfg, db, long_article_html(400))
    _prep(cfg, db, job_id)
    db.update_job(job_id, hold=1)
    assert db.claim_next_ready() is None

    db.update_job(job_id, hold=0)
    assert db.claim_next_ready()["id"] == job_id


def test_hold_for_review_holds_every_job(cfg, db):
    cfg.raw["pipeline"]["hold_for_review"] = True
    job_id = submit(cfg, db, long_article_html(400))
    job = _prep(cfg, db, job_id)
    assert job["state"] == "ready"
    assert job["hold"] == 1
    assert db.claim_next_ready() is None


def test_edits_to_spoken_txt_reach_the_audio(cfg, db):
    """The whole point of the pause: the operator can change the text."""
    job_id = submit(cfg, db, long_article_html(400))
    _prep(cfg, db, job_id)

    spoken = cfg.jobs_dir / job_id / "spoken.txt"
    spoken.write_text("## Edited heading\n\nThe operator rewrote this by hand.\n")

    run_job(db, cfg, job_id, RunOptions(from_stage="synthesizing"))
    assert db.get_job(job_id)["state"] == "done"

    from readcast.chunk import read_plan

    texts = " ".join(c.text for c in read_plan(cfg.jobs_dir / job_id))
    assert "The operator rewrote this by hand." in texts
    assert "Edited heading" in texts


def test_a_prep_failure_never_reaches_the_audio_lane(cfg, db):
    job_id = submit(cfg, db, "<html><body><p>too short</p></body></html>")
    try:
        _prep(cfg, db, job_id)
    except Exception:  # noqa: BLE001 - the row records the failure
        pass
    assert db.get_job(job_id)["state"] == "failed"
    assert db.claim_next_ready() is None


def test_the_two_lanes_hold_separate_locks(cfg, db):
    prep, audio = PrepWorker(db, cfg), Worker(db, cfg)
    assert prep.start()
    assert audio.start()          # a different lock file, so both run
    try:
        assert not PrepWorker(db, cfg).start()
        assert not Worker(db, cfg).start()
    finally:
        prep.stop()
        audio.stop()


def test_prep_runs_while_the_audio_lane_is_busy(cfg, db):
    """Submitting during a long render must still produce text promptly."""
    busy = submit(cfg, db, long_article_html(400))
    db.update_job(busy, state="synthesizing")  # the audio lane is occupied

    fresh = submit(cfg, db, long_article_html(400), "https://example.org/fresh")
    prep = PrepWorker(db, cfg, poll_seconds=0.05)
    assert prep.start()
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if db.get_job(fresh)["state"] in ("ready", "failed"):
                break
            time.sleep(0.1)
    finally:
        prep.stop()

    assert db.get_job(fresh)["state"] == "ready"
    assert (cfg.jobs_dir / fresh / "spoken.txt").is_file()
    # It did not disturb the job the audio lane was holding.
    assert db.get_job(busy)["state"] == "synthesizing"


def test_audio_from_an_older_render_is_adopted_for_resume(cfg, db):
    """Chunks written before sidecars existed must not be re-synthesized."""
    from readcast.worker import _adopt_existing_audio
    from readcast.chunk import Chunk

    chunks_dir = cfg.jobs_dir / "adopt" / "chunks"
    chunks_dir.mkdir(parents=True)
    plan = [
        Chunk(index=0, text="First chunk.", kind="body"),
        Chunk(index=1, text="Second chunk.", kind="body"),
    ]
    for c in plan:
        (chunks_dir / f"{c.index:03d}.wav").write_bytes(b"fake")

    assert _adopt_existing_audio(chunks_dir, plan, plan) == 2
    assert (chunks_dir / "000.txt").read_text() == "First chunk."

    # Text that changed is not adopted, so it gets re-synthesized.
    changed = [Chunk(index=0, text="Rewritten.", kind="body")]
    (chunks_dir / "000.txt").unlink()
    assert _adopt_existing_audio(chunks_dir, plan, changed) == 0
    assert not (chunks_dir / "000.txt").exists()


def test_an_older_database_gains_the_new_columns(tmp_path):
    """CREATE TABLE IF NOT EXISTS never alters an existing table."""
    import sqlite3

    from readcast.db import ADDED_COLUMNS, Database

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT NOT NULL,"
        " submitted_at TEXT NOT NULL, state TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO jobs (id, url, submitted_at, state)"
        " VALUES ('old-1', 'https://example.org/x', '2026-01-01T00:00:00+00:00', 'done')"
    )
    old.commit()
    old.close()

    db = Database(path)
    with db.connect() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert set(ADDED_COLUMNS) <= columns

    # The existing row survives and takes the defaults.
    job = db.get_job("old-1")
    assert job["url"] == "https://example.org/x"
    assert job["hold"] == 0
    assert job["in_feed"] == 1
    db.update_job("old-1", hold=1)
    assert db.get_job("old-1")["hold"] == 1


def test_a_worker_waits_for_a_lock_instead_of_giving_up(cfg, db):
    """A service restart races its predecessor for the lock.

    Giving up permanently leaves a server answering HTTP while nothing runs.
    """
    import time

    incumbent = Worker(db, cfg)
    assert incumbent.start()

    successor = Worker(db, cfg, poll_seconds=0.05)
    assert successor.start(wait_for_lock=True) is not False
    assert not successor.holds_lock          # the incumbent still has it
    try:
        incumbent.stop()                     # the old process finishes shutting down
        deadline = time.time() + 15
        while time.time() < deadline and not successor.holds_lock:
            time.sleep(0.2)
        assert successor.holds_lock, "the successor never picked up the freed lock"
    finally:
        successor.stop()


def test_a_waiting_worker_still_reaps_orphans_once_it_takes_over(cfg, db):
    import time

    job_id = submit(cfg, db, long_article_html(200))
    db.update_job(job_id, state="synthesizing")

    incumbent = Worker(db, cfg)
    assert incumbent.start()
    successor = Worker(db, cfg, poll_seconds=0.05)
    successor.start(wait_for_lock=True)
    try:
        incumbent.stop()
        deadline = time.time() + 15
        while time.time() < deadline and db.get_job(job_id)["state"] != "failed":
            time.sleep(0.2)
        assert db.get_job(job_id)["state"] == "failed"
        assert "interrupted" in db.get_job(job_id)["error"]
    finally:
        successor.stop()


def test_a_job_runs_end_to_end_with_a_populated_voice_pool(cfg, db):
    """The empty-pool path was the only one under test, and it hid a crash."""
    from readcast.voices import pool_dir, silence_reference

    pool = pool_dir(cfg)
    pool.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        silence_reference(pool / f"{i:02d}.wav")

    job_id = submit(cfg, db, long_article_html(400))
    run_job(db, cfg, job_id)

    job = db.get_job(job_id)
    assert job["state"] == "done"
    assert job["voice_main"], "the episode should have recorded its narrator"
    assert (cfg.jobs_dir / job_id / "episode.mp3").is_file()


# -- the hold-new switch -----------------------------------------------------


def test_the_switch_decides_whether_new_work_waits(cfg, db):
    """A runtime switch, because editing a file and restarting is not a switch."""
    from readcast.config import hold_new_jobs, set_hold_new_jobs

    cfg.raw["pipeline"]["hold_for_review"] = False
    assert hold_new_jobs(cfg, db) is False

    set_hold_new_jobs(db, True)
    assert hold_new_jobs(cfg, db) is True          # the switch overrides the file
    set_hold_new_jobs(db, False)
    assert hold_new_jobs(cfg, db) is False

    # With nothing stored, config.yml supplies the starting position.
    cfg.raw["pipeline"]["hold_for_review"] = True
    db.set_setting("hold_for_review", "")
    with db.connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'hold_for_review'")
    assert hold_new_jobs(cfg, db) is True


def test_a_new_job_waits_when_the_switch_is_on(cfg, db):
    from readcast.config import set_hold_new_jobs

    set_hold_new_jobs(db, True)
    job_id = submit(cfg, db, long_article_html(400))
    job = _prep(cfg, db, job_id)

    assert job["state"] == "ready"
    assert job["hold"] == 1
    assert db.claim_next_ready() is None
    # The text and the narrator are both settled, ready to be changed.
    assert (cfg.jobs_dir / job_id / "spoken.txt").is_file()


def test_a_new_job_goes_straight_through_when_it_is_off(cfg, db):
    from readcast.config import set_hold_new_jobs

    set_hold_new_jobs(db, False)
    job_id = submit(cfg, db, long_article_html(400))
    assert _prep(cfg, db, job_id)["hold"] == 0
    assert db.claim_next_ready()["id"] == job_id


def test_the_switch_survives_a_restart(cfg, db):
    """It lives in the database, not in the process."""
    from readcast.config import hold_new_jobs, set_hold_new_jobs
    from readcast.db import Database

    set_hold_new_jobs(db, True)
    assert hold_new_jobs(cfg, Database(cfg.db_path)) is True


def test_a_stopped_render_keeps_its_work_and_resumes(cfg, db):
    """Stopping is not failing: the chunks already made must survive."""
    from readcast.synth.runner import SynthesisCancelled
    from readcast.voices import pool_dir, silence_reference

    pool = pool_dir(cfg)
    pool.mkdir(parents=True, exist_ok=True)
    silence_reference(pool / "00.wav")

    job_id = submit(cfg, db, long_article_html(900))
    _prep(cfg, db, job_id)

    # Stop it once a few chunks are done.
    import readcast.synth.runner as runner

    original = runner.synthesize_chunks
    state = {"calls": 0}

    def stop_after_three(*args, **kwargs):
        def should_stop():
            state["calls"] += 1
            return state["calls"] > 3
        kwargs["should_stop"] = should_stop
        return original(*args, **kwargs)

    import readcast.worker as worker_mod

    worker_mod.synthesize_chunks = stop_after_three
    try:
        run_job(db, cfg, job_id, RunOptions(from_stage="synthesizing"))
    finally:
        worker_mod.synthesize_chunks = original

    job = db.get_job(job_id)
    assert job["state"] == "cancelled"
    assert "stopped after" in job["error"]
    made = list((cfg.jobs_dir / job_id / "chunks").glob("[0-9]*.wav"))
    assert made, "the audio already produced was thrown away"

    # Releasing it carries on rather than starting over.
    db.update_job(job_id, state="ready", hold=0, pending_from="synthesizing")
    run_job(db, cfg, job_id, RunOptions(from_stage="synthesizing"))
    assert db.get_job(job_id)["state"] == "done"
