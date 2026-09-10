"""The command line the operator actually uses."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from readcast.cli import app
from readcast.worker import run_job
from tests.test_pipeline import long_article_html, submit

runner = CliRunner()


def run(workspace, *args):
    return runner.invoke(app, [*args, "--config", str(workspace / "config.yml")])


def test_rules_test_exits_zero(workspace):
    result = run(workspace, "rules", "test")
    assert result.exit_code == 0, result.output
    assert "0 failed" in result.output


def test_rules_test_exits_non_zero_and_diffs_on_failure(workspace):
    tests_yml = workspace / "rules" / "tests.yml"
    tests_yml.write_text(
        'cases:\n  - name: deliberately wrong\n    in: "It shipped in 1984."\n'
        '    out: "something else entirely"\n'
    )
    result = run(workspace, "rules", "test")
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "nineteen eighty-four" in result.output
    assert "---" in result.output  # a unified diff


def test_init_and_feed_url(workspace, cfg, db):
    result = run(workspace, "init")
    assert result.exit_code == 0
    assert (cfg.feed_dir / "feed.xml").is_file()
    assert (cfg.feed_dir / "cover.jpg").is_file()

    url = run(workspace, "feed", "url")
    assert url.exit_code == 0
    assert url.output.strip().startswith("https://readcast.test/f/")


def test_feed_rotate_changes_the_url(workspace, cfg, db):
    before = db.feed_token()
    result = run(workspace, "feed", "rotate")
    assert result.exit_code == 0
    assert before not in result.output
    assert db.feed_token() != before


def test_feed_rebuild_is_idempotent(workspace, cfg, db):
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    first = (cfg.feed_dir / "feed.xml").read_bytes()
    (cfg.feed_dir / "feed.xml").unlink()
    result = run(workspace, "feed", "rebuild")
    assert result.exit_code == 0
    assert (cfg.feed_dir / "feed.xml").read_bytes() == first


def test_bookmarklet_carries_the_host_and_token(workspace):
    result = run(workspace, "bookmarklet")
    assert result.exit_code == 0
    code = result.output.splitlines()[0]
    assert code.startswith("javascript:(async()=>")
    assert "B='https://readcast.test'" in code
    assert "T='test-token'" in code
    assert "client_html" in code
    assert "\n" not in code  # one line, for the bookmark field


def test_bookmarklet_falls_back_to_a_form_post(workspace):
    """A page whose CSP blocks fetch still has to work."""
    code = run(workspace, "bookmarklet").output.splitlines()[0]
    assert "catch" in code
    assert "/jobs/form" in code
    assert "f.method='POST'" in code
    assert "target='_blank'" in code


def test_bookmarklet_base_override(workspace):
    """The browser posts to loopback; the feed keeps its LAN address."""
    result = run(workspace, "bookmarklet", "--base", "http://127.0.0.1:8788")
    code = result.output.splitlines()[0]
    assert "B='http://127.0.0.1:8788'" in code
    assert "readcast.test" not in code
    assert "warning" not in result.output.lower()


def test_jobs_listing(workspace, cfg, db):
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    result = run(workspace, "jobs")
    assert result.exit_code == 0
    assert job_id in result.output
    assert "done" in result.output

    as_json = run(workspace, "jobs", "--json")
    assert json.loads(as_json.output)[0]["id"] == job_id

    empty = run(workspace, "jobs", "--state", "failed")
    assert "no jobs" in empty.output


def test_lexicon_suggest_aggregates_unknowns(workspace, cfg, db):
    from tests.conftest import fixture_html

    job_id = submit(cfg, db, fixture_html("tech_review"), "https://arstechnica.com/x")
    run_job(db, cfg, job_id)

    result = run(workspace, "lexicon", "suggest", "--min-count", "1")
    assert result.exit_code == 0
    assert "term" in result.output and "suggested" in result.output
    assert "H100" in result.output
    # It never edits the lexicon itself.
    assert "Add the ones worth fixing" in result.output
    assert "H100" not in (workspace / "rules" / "lexicon.yml").read_text()


def test_lexicon_suggest_respects_min_count(workspace, cfg, db):
    from tests.conftest import fixture_html

    job_id = submit(cfg, db, fixture_html("tech_review"), "https://arstechnica.com/x")
    run_job(db, cfg, job_id)
    result = run(workspace, "lexicon", "suggest", "--min-count", "500")
    assert "nothing above" in result.output


def test_preview_writes_a_scratch_file(workspace, cfg, db):
    job_id = submit(cfg, db, long_article_html(1500))
    run_job(db, cfg, job_id)
    result = run(workspace, "preview", job_id, "--minutes", "0.2")
    assert result.exit_code == 0, result.output
    assert (cfg.jobs_dir / job_id / "preview" / "episode.mp3").is_file()


def test_say_synthesizes_each_candidate(workspace, tmp_path):
    result = run(
        workspace, "say", "Kubernetes",
        "--compare", "koo-ber-net-ees", "--compare", "cube-er-net-ees",
        "--backend", "test",
    )
    assert result.exit_code == 0, result.output
    assert "koo-ber-net-ees" in result.output
    assert "cube-er-net-ees" in result.output
    assert result.output.count(".wav") == 3


def test_prep_stops_at_ready_without_making_audio(workspace, cfg, db):
    from tests.conftest import fixture_html

    job_id = submit(cfg, db, fixture_html("tech_review"), "https://arstechnica.com/x")
    result = run(workspace, "prep", job_id)
    assert result.exit_code == 0, result.output
    assert "ready" in result.output
    assert db.get_job(job_id)["state"] == "ready"
    assert (cfg.jobs_dir / job_id / "spoken.txt").is_file()
    assert not (cfg.jobs_dir / job_id / "episode.mp3").exists()


def test_hold_and_release(workspace, cfg, db):
    from tests.conftest import fixture_html

    job_id = submit(cfg, db, fixture_html("tech_review"), "https://arstechnica.com/x")
    run(workspace, "prep", job_id)

    assert run(workspace, "hold", job_id).exit_code == 0
    assert db.get_job(job_id)["hold"] == 1
    assert db.claim_next_ready() is None

    assert run(workspace, "release", job_id).exit_code == 0
    assert db.get_job(job_id)["hold"] == 0
    assert db.claim_next_ready()["id"] == job_id


def test_hold_and_release_reject_unknown_jobs(workspace):
    assert run(workspace, "hold", "nope").exit_code == 1
    assert run(workspace, "release", "nope").exit_code == 1


def test_show_raw_prints_just_the_text(workspace, cfg, db):
    from tests.conftest import fixture_html

    job_id = submit(cfg, db, fixture_html("tech_review"), "https://arstechnica.com/x")
    run(workspace, "prep", job_id)
    result = run(workspace, "show", job_id, "--raw")
    assert result.exit_code == 0
    assert result.output.startswith("## ")
    assert result.output == (cfg.jobs_dir / job_id / "spoken.txt").read_text()
