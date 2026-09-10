"""The watch dashboard reads state; it must never write any."""

from __future__ import annotations

import pytest

from readcast.dashboard import (
    Progress, bar, chunk_progress, check_services, job_artifacts, job_detail,
    listed_jobs, render, _clock, _hms,
)
from readcast.db import new_job_id
from readcast.worker import run_job
from tests.test_pipeline import long_article_html, submit


def test_time_formatting():
    assert _hms(None) == "—"
    assert _hms(45) == "45s"
    assert _hms(125) == "2m05s"
    assert _hms(7325) == "2h02m"
    assert _clock(391) == "6:31"
    assert _clock(0) == "—"


def test_bar_fills_proportionally():
    assert str(bar(0.0, width=10)).count("█") == 0
    assert str(bar(0.5, width=10)).count("█") == 5
    assert str(bar(1.0, width=10)).count("█") == 10


def test_chunk_progress_reads_the_worker_output(cfg, db):
    job_id = submit(cfg, db, long_article_html(400))
    run_job(db, cfg, job_id)
    progress = chunk_progress(cfg.jobs_dir / job_id)
    assert progress.total > 0
    assert progress.done == progress.total
    assert progress.fraction == 1.0


def test_progress_of_a_job_with_no_plan_is_empty(cfg, db, tmp_path):
    assert chunk_progress(tmp_path).total == 0
    assert chunk_progress(tmp_path).fraction == 0.0


def test_eta_needs_a_measured_rate():
    assert Progress(done=5, total=10).eta_s is None
    assert Progress(done=5, total=10, per_chunk=2.0).eta_s == 10.0
    assert Progress(done=10, total=10, per_chunk=2.0).eta_s == 0.0


def test_render_covers_idle_running_and_finished(cfg, db):
    from rich.console import Console

    def draw() -> str:
        console = Console(width=100, record=True, file=open("/dev/null", "w"))
        console.print(render(cfg, db, {"api 8788": True, "speech 8770": False}))
        return console.export_text()

    assert "idle" in draw()  # nothing queued or running yet

    job_id = submit(cfg, db, long_article_html(400))
    db.update_job(job_id, state="queued", title="A queued piece")
    assert "A queued piece" in draw()

    db.update_job(job_id, state="queued")
    run_job(db, cfg, job_id)
    output = draw()
    assert job_id in output
    assert "done" in output
    assert "1 episode" in output


def test_check_services_reports_both_ports(cfg):
    services = check_services(cfg)
    assert len(services) == 2
    assert any("api" in name for name in services)
    assert any("speech" in name for name in services)
    assert all(isinstance(v, bool) for v in services.values())


def test_watch_once_does_not_write_to_the_database(cfg, db):
    from readcast.dashboard import watch

    job_id = submit(cfg, db, long_article_html(300))
    before = db.get_job(job_id)
    watch(cfg, db, once=True)
    assert db.get_job(job_id) == before


def _draw(renderable) -> str:
    from rich.console import Console

    console = Console(width=100, record=True, file=open("/dev/null", "w"))
    console.print(renderable)
    return console.export_text()


def test_detail_shows_the_spoken_text_for_a_finished_job(cfg, db):
    job_id = submit(cfg, db, long_article_html(400))
    run_job(db, cfg, job_id)
    job = db.get_job(job_id)
    output = _draw(job_detail(cfg, db, job))
    assert "spoken.txt" in output
    spoken = (cfg.jobs_dir / job_id / "spoken.txt").read_text()
    assert spoken.split()[0] in output
    assert job_id in output


def test_detail_explains_that_a_queued_job_has_no_text_yet(cfg, db):
    job_id = db.create_job(id=new_job_id(), url="https://example.org/not-yet", title="Later")
    output = _draw(job_detail(cfg, db, db.get_job(job_id)))
    assert "No text yet" in output
    assert "preparing" in output
    assert "https://example.org/not-yet" in output


def test_detail_falls_back_to_extracted_markdown_before_preparation(cfg, db, tmp_path):
    job_id = submit(cfg, db, long_article_html(400))
    d = cfg.jobs_dir / job_id
    (d / "extracted.md").write_text("## A heading\n\nSome extracted prose.")
    output = _draw(job_detail(cfg, db, db.get_job(job_id)))
    assert "extracted.md" in output
    assert "Some extracted prose" in output


def test_chunk_view_lists_what_each_call_receives(cfg, db):
    job_id = submit(cfg, db, long_article_html(500))
    run_job(db, cfg, job_id)
    output = _draw(job_detail(cfg, db, db.get_job(job_id), chunks=True))
    assert "chunks" in output
    assert "intro" in output
    assert "body" in output


def test_listed_jobs_is_newest_first_even_within_one_second(cfg, db):
    ids = [db.create_job(id=new_job_id(), url=f"https://example.org/{i}") for i in range(4)]
    assert [j["id"] for j in listed_jobs(db)] == list(reversed(ids))
    output = _draw(render(cfg, db, {"api": True, "speech": True}))
    assert "1-9 view text" in output


def test_job_artifacts_point_into_the_job_directory(cfg, db):
    job_id = submit(cfg, db, long_article_html(300))
    files = job_artifacts(cfg, db.get_job(job_id))
    assert files["spoken"].name == "spoken.txt"
    assert files["plan"].parent.name == "chunks"
    assert files["spoken"].parent == cfg.jobs_dir / job_id


def test_the_terminal_queue_matches_the_audio_lane_order(cfg, db):
    """The dashboard must show run order, not arrival order."""
    from readcast.worker import RunOptions, run_job

    ids = []
    for i in range(3):
        job_id = submit(cfg, db, long_article_html(300), f"https://example.org/{i}")
        db.update_job(job_id, title=f"Article {i}", title_locked=1)
        run_job(db, cfg, job_id, RunOptions(from_stage="fetching", until_stage="preparing"))
        ids.append(job_id)

    db.set_queue_order([ids[2], ids[0], ids[1]])
    output = _draw(render(cfg, db, {"api": True, "speech": True}))
    positions = [output.index(f"Article {i}") for i in (2, 0, 1)]
    assert positions == sorted(positions), "queue is not shown in run order"
    assert db.claim_next_ready()["id"] == ids[2]


def test_the_rate_starts_from_a_prior_and_decays_toward_what_it_sees():
    from readcast.dashboard import DEFAULT_SECONDS_PER_CHUNK, blend_rate

    # With nothing measured, the estimate is the prior.
    assert blend_rate([], 50.0) == 50.0

    # Consistent evidence pulls it toward the truth.
    rate = blend_rate([20.0] * 20, 50.0)
    assert 20.0 <= rate < 22.0

    # Recent chunks weigh more than old ones.
    speeding_up = blend_rate([60.0] * 10 + [10.0] * 10, 50.0)
    slowing_down = blend_rate([10.0] * 10 + [60.0] * 10, 50.0)
    assert speeding_up < slowing_down

    assert DEFAULT_SECONDS_PER_CHUNK > 0


def test_reused_chunks_do_not_flatter_the_rate():
    """A hardlinked chunk appears instantly and proves nothing about speed."""
    from readcast.dashboard import blend_rate

    assert blend_rate([0.01] * 50, 50.0) == 50.0
    assert blend_rate([0.01, 0.01, 40.0], 50.0) < 50.0


def test_one_stalled_chunk_does_not_redefine_the_estimate():
    from readcast.dashboard import blend_rate

    # A machine paused for an hour mid-run.
    assert blend_rate([50.0] * 5 + [3600.0], 50.0) < 150.0


def test_progress_measures_only_chunks_made_in_this_run(cfg, db, tmp_path):
    """Reused chunks keep their original mtime, hours in the past."""
    import os
    import time

    from readcast.dashboard import chunk_progress

    chunks = tmp_path / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "plan.jsonl").write_text(
        "\n".join('{"index": %d, "text": "t", "kind": "body"}' % i for i in range(10))
    )
    started = (chunks / "plan.jsonl").stat().st_mtime

    for i in range(4):  # adopted from an earlier render
        old = chunks / f"{i:03d}.wav"
        old.write_bytes(b"x")
        os.utime(old, (started - 9000, started - 9000))
    for i in range(4, 6):  # made just now, 30 s apart
        fresh = chunks / f"{i:03d}.wav"
        fresh.write_bytes(b"x")
        os.utime(fresh, (started + 30 * (i - 3), started + 30 * (i - 3)))

    progress = chunk_progress(tmp_path, prior=50.0)
    assert progress.done == 6
    assert progress.fresh == 2
    assert 30 <= progress.per_chunk <= 50   # pulled toward the observed 30s
    assert progress.eta_s == pytest.approx(4 * progress.per_chunk)
    assert progress.finish_at > time.time()


def test_the_rate_is_learned_per_backend(cfg, db):
    """Backends are two orders of magnitude apart; one shared average is wrong
    for whichever is not in use."""
    from readcast.dashboard import (
        BACKEND_SECONDS_PER_CHUNK, record_chunk_rate, seconds_per_chunk,
    )

    # With nothing learned, each backend starts from its own measured default.
    assert seconds_per_chunk(db, "mlx") == BACKEND_SECONDS_PER_CHUNK["mlx"]
    assert seconds_per_chunk(db, "kokoro") == BACKEND_SECONDS_PER_CHUNK["kokoro"]
    assert seconds_per_chunk(db, "mlx") > seconds_per_chunk(db, "kokoro") * 10

    record_chunk_rate(db, seconds=620.0, chunks=10, backend="mlx")      # 62 s/chunk
    record_chunk_rate(db, seconds=6.0, chunks=10, backend="kokoro")     # 0.6 s/chunk
    assert seconds_per_chunk(db, "mlx") == pytest.approx(62.0)
    assert seconds_per_chunk(db, "kokoro") == pytest.approx(0.6)

    # Learning one does not disturb the other.
    record_chunk_rate(db, seconds=6.0, chunks=10, backend="kokoro")
    assert seconds_per_chunk(db, "mlx") == pytest.approx(62.0)


def test_the_learned_rate_is_persisted_and_reused(cfg, db):
    from readcast.dashboard import (
        DEFAULT_SECONDS_PER_CHUNK, record_chunk_rate, seconds_per_chunk,
    )

    assert seconds_per_chunk(db) == DEFAULT_SECONDS_PER_CHUNK
    record_chunk_rate(db, seconds=200.0, chunks=10)   # 20 s/chunk observed
    assert seconds_per_chunk(db) == pytest.approx(20.0)

    record_chunk_rate(db, seconds=600.0, chunks=10)   # 60 s/chunk observed
    assert 20.0 < seconds_per_chunk(db) < 60.0        # blended, not replaced

    # Nonsense input never corrupts the stored value.
    before = seconds_per_chunk(db)
    assert record_chunk_rate(db, seconds=0, chunks=0) is None
    assert seconds_per_chunk(db) == before


def test_finish_time_is_a_clock_time():
    import time

    from readcast.dashboard import when

    assert when(None) == "—"
    soon = when(time.time() + 600)
    assert ":" in soon and len(soon) == 5
    # Build tomorrow explicitly: a fixed offset lands two days out late at night.
    from datetime import datetime, timedelta

    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=30)
    assert when(tomorrow.timestamp()) == "tomorrow 09:30"
    later = (datetime.now() + timedelta(days=4)).replace(hour=9, minute=30)
    assert "tomorrow" not in when(later.timestamp())


def test_progress_follows_the_worker_not_the_directory(cfg, db, tmp_path):
    """A rerender overwrites 000.wav upward: the file count sits still."""
    import json
    import time

    from readcast.dashboard import chunk_progress

    chunks = tmp_path / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "plan.jsonl").write_text(
        "\n".join('{"index": %d, "text": "t", "kind": "body"}' % i for i in range(600))
    )
    # 175 files exist from an earlier render; the worker is at 400 overwriting them.
    for i in range(175):
        (chunks / f"{i:03d}.wav").write_bytes(b"x")
    now = time.time()
    (chunks / "progress.json").write_text(json.dumps({
        "done": 400, "total": 600, "reused": 0, "started": now - 4000,
        "updated": now, "stamps": [now - 90, now - 60, now - 30, now],
    }))

    progress = chunk_progress(tmp_path, prior=50.0)
    assert progress.done == 400, "the dashboard should trust the worker's count"
    assert progress.total == 600
    assert 25 <= progress.per_chunk <= 50    # pulled toward the observed 30s
    assert progress.eta_s == pytest.approx(200 * progress.per_chunk)


def test_a_stale_report_is_ignored(cfg, tmp_path):
    """A worker that died must not leave a frozen number on screen."""
    import json
    import time

    from readcast.dashboard import chunk_progress

    chunks = tmp_path / "chunks"
    chunks.mkdir(parents=True)
    (chunks / "plan.jsonl").write_text(
        "\n".join('{"index": %d, "text": "t", "kind": "body"}' % i for i in range(10))
    )
    for i in range(3):
        (chunks / f"{i:03d}.wav").write_bytes(b"x")
    (chunks / "progress.json").write_text(json.dumps({
        "done": 9, "total": 10, "started": 0, "updated": time.time() - 3600, "stamps": [],
    }))

    progress = chunk_progress(tmp_path, prior=50.0)
    assert progress.done == 3, "a stale report should give way to the directory"
