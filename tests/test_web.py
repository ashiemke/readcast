"""The web dashboard: state, reordering, and editing the text in place."""

from __future__ import annotations

import pytest

from readcast.db import new_job_id
from readcast.worker import RunOptions, run_job
from tests.test_pipeline import long_article_html, submit


def _ready(cfg, db, url="https://example.org/x", title=None):
    job_id = submit(cfg, db, long_article_html(400), url)
    if title:
        db.update_job(job_id, title=title, title_locked=1)   # a chosen title
    run_job(db, cfg, job_id, RunOptions(from_stage="fetching", until_stage="preparing"))
    return job_id


def test_ui_page_serves_without_a_token(client):
    client.headers.pop("authorization")
    response = client.get("/ui")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "readcast" in response.text
    # The page carries no secret; it asks the browser for one.
    assert "api token" in response.text


def test_state_endpoint_needs_a_token(client):
    client.headers.pop("authorization")
    assert client.get("/api/state").status_code == 401


def test_state_reports_the_queue_and_the_running_job(cfg, db, client):
    first = _ready(cfg, db, "https://example.org/1", "First piece")
    second = _ready(cfg, db, "https://example.org/2", "Second piece")
    db.update_job(second, state="synthesizing")

    state = client.get("/api/state").json()
    assert state["now"]["id"] == second
    assert state["now"]["title"] == "Second piece"
    assert [j["id"] for j in state["ready"]] == [first]
    assert state["ready"][0]["words"] > 0
    assert state["ready"][0]["editable"] is True
    assert state["feed_url"].startswith("https://readcast.test/f/")
    assert "episodes" in state and "audio_s" in state


def test_reordering_changes_what_runs_next(cfg, db, client):
    a = _ready(cfg, db, "https://example.org/a", "A")
    b = _ready(cfg, db, "https://example.org/b", "B")
    c = _ready(cfg, db, "https://example.org/c", "C")
    assert [j["id"] for j in db.ready_queue()] == [a, b, c]

    response = client.post("/api/queue/order", json={"order": [c, a, b]})
    assert response.status_code == 200
    assert response.json()["order"] == [c, a, b]
    assert [j["id"] for j in db.ready_queue()] == [c, a, b]
    # The audio lane honours it.
    assert db.claim_next_ready()["id"] == c


def test_a_newly_prepared_job_joins_the_back_of_the_queue(cfg, db, client):
    a = _ready(cfg, db, "https://example.org/a")
    b = _ready(cfg, db, "https://example.org/b")
    client.post("/api/queue/order", json={"order": [b, a]})
    late = _ready(cfg, db, "https://example.org/late")
    assert [j["id"] for j in db.ready_queue()] == [b, a, late]


def test_reading_and_editing_the_text(cfg, db, client):
    job_id = _ready(cfg, db)
    original = client.get(f"/api/jobs/{job_id}/text").json()
    assert original["exists"] and original["editable"]
    assert original["text"].startswith("## ")

    edited = "## Rewritten heading\n\nA line the operator typed in the browser.\n"
    response = client.put(f"/api/jobs/{job_id}/text", json={"text": edited})
    assert response.status_code == 200
    assert response.json()["words"] == 10  # the "##" marker is not a word

    on_disk = (cfg.jobs_dir / job_id / "spoken.txt").read_text()
    assert on_disk == edited
    assert db.get_job(job_id)["word_count"] == 10


def test_a_trailing_newline_is_added_so_the_file_stays_well_formed(cfg, db, client):
    job_id = _ready(cfg, db)
    client.put(f"/api/jobs/{job_id}/text", json={"text": "No trailing newline"})
    assert (cfg.jobs_dir / job_id / "spoken.txt").read_text().endswith("\n")


def test_editing_is_refused_while_the_audio_lane_holds_the_job(cfg, db, client):
    job_id = _ready(cfg, db)
    db.update_job(job_id, state="synthesizing")
    before = (cfg.jobs_dir / job_id / "spoken.txt").read_text()

    response = client.put(f"/api/jobs/{job_id}/text", json={"text": "clobbered"})
    assert response.status_code == 409
    assert "hold it first" in response.json()["detail"]
    assert (cfg.jobs_dir / job_id / "spoken.txt").read_text() == before
    assert client.get(f"/api/jobs/{job_id}/text").json()["editable"] is False


def test_text_endpoints_404_on_an_unknown_job(client):
    assert client.get("/api/jobs/nope/text").status_code == 404
    assert client.put("/api/jobs/nope/text", json={"text": "x"}).status_code == 404


def test_a_job_with_no_text_reports_that_honestly(cfg, db, client):
    job_id = db.create_job(id=new_job_id(), url="https://example.org/unprepared")
    body = client.get(f"/api/jobs/{job_id}/text").json()
    assert body["exists"] is False
    assert body["text"] == ""
    assert body["editable"] is False


def test_hold_and_release_over_http(cfg, db, client):
    job_id = _ready(cfg, db)
    assert client.post(f"/jobs/{job_id}/hold").json()["hold"] is True
    assert db.claim_next_ready() is None
    assert client.post(f"/jobs/{job_id}/hold?release=true").json()["hold"] is False
    assert db.claim_next_ready()["id"] == job_id


def test_edited_text_is_what_gets_synthesized(cfg, db, client):
    """The edit has to survive all the way into the chunk plan."""
    job_id = _ready(cfg, db)
    client.put(
        f"/api/jobs/{job_id}/text",
        json={"text": "## Browser heading\n\nThe operator typed this in the browser.\n"},
    )
    run_job(db, cfg, job_id, RunOptions(from_stage="synthesizing"))

    from readcast.chunk import read_plan

    texts = " ".join(c.text for c in read_plan(cfg.jobs_dir / job_id))
    assert "The operator typed this in the browser." in texts
    assert db.get_job(job_id)["state"] == "done"


def test_the_page_cannot_be_widened_by_a_long_title(client):
    """A long title must truncate, not push the table past its container."""
    page = client.get("/ui").text
    assert "table-layout:fixed" in page
    assert "<colgroup>" in page
    assert "text-overflow:ellipsis" in page
    # The panel clips anything that still tries to escape, and wide tables scroll.
    assert "overflow:hidden" in page
    assert "overflow-x:auto" in page


def test_the_page_makes_no_external_requests(client):
    """It is served from the tailnet; it must not need the internet."""
    import re

    page = client.get("/ui").text
    for url in re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)', page):
        assert not url.startswith(("http://", "https://", "//")), url


# -- the editor offers only what will actually happen ------------------------


def test_text_endpoint_reports_what_a_rerender_would_cost(cfg, db, client):
    job_id = _ready(cfg, db)
    from readcast.worker import RunOptions, run_job

    run_job(db, cfg, job_id, RunOptions(from_stage="synthesizing"))

    body = client.get(f"/api/jobs/{job_id}/text").json()
    assert body["state"] == "done"
    assert body["chunks"] > 0
    assert body["rerender_s"] > 0
    assert body["hold"] is False


def test_the_cost_estimate_survives_a_job_with_no_chunk_plan(cfg, db, client):
    """A ready job has text but has never been chunked."""
    job_id = _ready(cfg, db)
    body = client.get(f"/api/jobs/{job_id}/text").json()
    assert body["chunks"] >= 1
    assert body["rerender_s"] > 0


def test_hold_state_is_reported_for_the_release_button(cfg, db, client):
    job_id = _ready(cfg, db)
    assert client.get(f"/api/jobs/{job_id}/text").json()["hold"] is False
    client.post(f"/jobs/{job_id}/hold")
    assert client.get(f"/api/jobs/{job_id}/text").json()["hold"] is True


def test_release_only_clears_the_hold_it_does_not_requeue(cfg, db, client):
    """It is deliberately not the CLI's `release`: a click must not start hours
    of synthesis. Finished episodes use the separate, confirmed Re-render."""
    job_id = _ready(cfg, db)
    from readcast.worker import RunOptions, run_job

    run_job(db, cfg, job_id, RunOptions(from_stage="synthesizing"))
    assert db.get_job(job_id)["state"] == "done"

    client.post(f"/jobs/{job_id}/hold?release=true")
    assert db.get_job(job_id)["state"] == "done"      # unchanged
    assert db.claim_next_ready() is None              # nothing was queued


def test_the_editor_hides_buttons_that_would_do_nothing(client):
    page = client.get("/ui").text
    # Each control is gated on the job's state rather than always shown.
    for control in ("ed-save", "ed-rules", "ed-hold", "ed-release", "ed-rerender"):
        assert f'$("{control}").hidden' in page
    assert 'data.state === "ready"' in page
    assert 'data.state === "done"' in page
    # Re-render names its cost and asks first.
    assert "Re-render (~" in page
    assert "confirm(" in page
    assert "rerender?from=synthesizing" in page


# -- hearing and changing the narrator from the queue ------------------------


def _with_pool(cfg, count=4):
    from readcast.voices import pool_dir, silence_reference

    pool = pool_dir(cfg)
    pool.mkdir(parents=True, exist_ok=True)
    return [silence_reference(pool / f"{i:02d}.wav", seconds=0.6 + i * 0.1)
            for i in range(count)]


def test_a_ready_job_already_has_a_narrator(cfg, db, client):
    """Cast at ready, not at synthesis, so the queue can show it."""
    _with_pool(cfg)
    job_id = _ready(cfg, db)
    assert db.get_job(job_id)["voice_main"]
    row = next(j for j in client.get("/api/state").json()["ready"] if j["id"] == job_id)
    assert row["voice"] == db.get_job(job_id)["voice_main"]


def test_rerolling_picks_a_different_narrator(cfg, db, client):
    _with_pool(cfg)
    job_id = _ready(cfg, db)
    first = db.get_job(job_id)["voice_main"]

    result = client.post(f"/api/jobs/{job_id}/voice/reroll").json()
    assert result["voice"] != first
    assert db.get_job(job_id)["voice_main"] == result["voice"]
    assert result["quote"] != result["voice"]


def test_a_narrator_can_be_named_outright(cfg, db, client):
    pool = _with_pool(cfg)
    job_id = _ready(cfg, db)
    result = client.post(f"/api/jobs/{job_id}/voice/reroll?name={pool[2].name}").json()
    assert result["voice"] == pool[2].name


def test_rerolling_with_one_voice_says_so(cfg, db, client):
    _with_pool(cfg, count=1)
    job_id = _ready(cfg, db)
    response = client.post(f"/api/jobs/{job_id}/voice/reroll")
    assert response.status_code == 409
    assert "voices sample" in response.json()["detail"]


def test_the_narrator_cannot_change_mid_render(cfg, db, client):
    _with_pool(cfg)
    job_id = _ready(cfg, db)
    before = db.get_job(job_id)["voice_main"]
    db.update_job(job_id, state="synthesizing")
    response = client.post(f"/api/jobs/{job_id}/voice/reroll")
    assert response.status_code == 409
    assert db.get_job(job_id)["voice_main"] == before


def test_the_sample_clip_is_served_for_playback(cfg, db, client):
    _with_pool(cfg)
    job_id = _ready(cfg, db)
    response = client.get(f"/api/jobs/{job_id}/voice.wav")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"

    bare = db.create_job(id=new_job_id(), url="https://example.org/none")
    assert client.get(f"/api/jobs/{bare}/voice.wav").status_code == 404


def test_the_pool_is_listed_with_pitches(cfg, db, client):
    _with_pool(cfg)
    pool = client.get("/api/voices").json()["pool"]
    assert len(pool) == 4
    assert all("name" in v for v in pool)


def test_the_queue_row_offers_play_and_reroll(client):
    page = client.get("/ui").text
    assert "data-play=" in page and "data-reroll=" in page
    assert "voice/reroll" in page
    # The clip is fetched with the header and played from a blob, so the token
    # never travels in a URL.
    assert "createObjectURL" in page
    assert "voice.wav?token" not in page


def test_the_play_button_becomes_a_stop_control(client):
    """A sample runs several seconds; there has to be a way out of it."""
    page = client.get("/ui").text
    assert "const PLAY" in page and "PAUSE =" in page
    assert "syncPlayButtons" in page
    # A second press on the same row stops rather than restarting.
    assert "if (audio.paused)" in page
    assert "audio.pause()" in page
    # The queue redraws every couple of seconds; the button state must survive.
    assert page.count("syncPlayButtons()") >= 2
    assert "renderRecent(state.recent);\n  syncPlayButtons();" in page
    # Blob URLs are released rather than leaked on every sample.
    assert "revokeObjectURL" in page


# -- choosing fast or good per episode ---------------------------------------


def test_the_backend_is_not_stamped_at_submission(cfg, db, client):
    """An unrendered job should follow the configured default, not the one that
    happened to be set when it was submitted."""
    response = client.post("/jobs", json={"url": "https://example.org/x"})
    job = db.get_job(response.json()["id"])
    assert job["backend"] is None

    from readcast.voices import effective_backend

    assert effective_backend(cfg, job) == cfg["tts"]["backend"]


def test_switching_a_job_between_fast_and_good(cfg, db, client):
    cfg.raw["tts"]["backends"]["kokoro"] = {"voices": ["am_michael", "bf_emma"]}
    job_id = _ready(cfg, db)

    result = client.post(f"/api/jobs/{job_id}/backend?name=kokoro").json()
    assert result["backend"] == "kokoro"
    assert db.get_job(job_id)["backend"] == "kokoro"
    assert result["voice"] in ("am_michael", "bf_emma")

    # Switching back drops the named voice: the two share no speakers.
    result = client.post(f"/api/jobs/{job_id}/backend?name=mlx").json()
    assert result["backend"] == "mlx"
    assert result["voice"] not in ("am_michael", "bf_emma")


def test_an_unknown_backend_is_refused(cfg, db, client):
    job_id = _ready(cfg, db)
    assert client.post(f"/api/jobs/{job_id}/backend?name=nonsense").status_code == 400
    assert client.post("/api/jobs/nope/backend?name=kokoro").status_code == 404


def test_the_backend_cannot_change_mid_render(cfg, db, client):
    job_id = _ready(cfg, db)
    db.update_job(job_id, state="synthesizing")
    assert client.post(f"/api/jobs/{job_id}/backend?name=kokoro").status_code == 409


def test_a_stale_voice_is_not_shown_after_a_backend_change(cfg, db, client):
    """A Breeze clip name on a job that a voice pack will read is a lie."""
    cfg.raw["tts"]["backends"]["kokoro"] = {"voices": ["am_michael"]}
    job_id = _ready(cfg, db)
    db.update_job(job_id, backend="kokoro", voice_main="05.wav")

    row = next(j for j in client.get("/api/state").json()["ready"] if j["id"] == job_id)
    assert row["voice"] is None
    assert row["backend"] == "kokoro"


def test_rerolling_follows_the_job_s_backend(cfg, db, client):
    cfg.raw["tts"]["backends"]["kokoro"] = {"voices": ["am_michael", "bf_emma", "bm_george"]}
    job_id = _ready(cfg, db)
    client.post(f"/api/jobs/{job_id}/backend?name=kokoro")
    result = client.post(f"/api/jobs/{job_id}/voice/reroll").json()
    assert result["voice"] in ("am_michael", "bf_emma", "bm_george")
    assert not result["voice"].endswith(".wav")


def test_the_queue_row_offers_the_fast_good_switch(client):
    page = client.get("/ui").text
    assert "data-backend=" in page
    assert '/backend?name=' in page
    assert ">fast<" in page or '"fast"' in page


def test_the_hold_new_switch_over_http(cfg, db, client):
    assert client.post("/api/settings/hold-new?on=true").json()["hold_new"] is True
    assert client.get("/api/state").json()["hold_new"] is True
    assert client.post("/api/settings/hold-new?on=false").json()["hold_new"] is False
    assert client.get("/api/state").json()["hold_new"] is False


def test_the_dashboard_shows_the_switch(client):
    page = client.get("/ui").text
    assert 'id="holdnew"' in page
    assert "settings/hold-new" in page
    assert "new: hold" in page and "new: auto" in page


# -- hand-edited text is not disposable --------------------------------------


def test_preparation_does_not_discard_a_hand_edit(cfg, db, client):
    """Rules output is reproducible; the operator's typing is not."""
    from readcast.worker import RunOptions, run_job

    job_id = _ready(cfg, db)
    edited = "## My own heading\n\nEvery word of this was typed by a person.\n"
    client.put(f"/api/jobs/{job_id}/text", json={"text": edited})
    assert db.get_job(job_id)["text_edited"] == 1

    # Anything that would re-run preparation leaves the edit alone.
    run_job(db, cfg, job_id, RunOptions(from_stage="preparing", until_stage="preparing"))
    assert (cfg.jobs_dir / job_id / "spoken.txt").read_text() == edited

    run_job(db, cfg, job_id, RunOptions(from_stage="fetching"))
    assert (cfg.jobs_dir / job_id / "spoken.txt").read_text() == edited
    assert db.get_job(job_id)["state"] == "done"

    from readcast.chunk import read_plan

    spoken = " ".join(c.text for c in read_plan(cfg.jobs_dir / job_id))
    assert "Every word of this was typed by a person." in spoken


def test_asking_for_the_rules_again_does_replace_the_edit(cfg, db, client):
    from readcast.worker import RunOptions, run_job

    job_id = _ready(cfg, db)
    client.put(f"/api/jobs/{job_id}/text", json={"text": "Typed by hand.\n"})

    # "Re-run rules" is the explicit request to throw the edit away.
    client.post(f"/jobs/{job_id}/rerender?from=preparing")
    assert db.get_job(job_id)["text_edited"] == 0

    run_job(db, cfg, job_id, RunOptions(from_stage="preparing", until_stage="preparing"))
    assert (cfg.jobs_dir / job_id / "spoken.txt") .read_text() != "Typed by hand.\n"


def test_forcing_preparation_overwrites_deliberately(cfg, db, client):
    from readcast.worker import RunOptions, run_job

    job_id = _ready(cfg, db)
    client.put(f"/api/jobs/{job_id}/text", json={"text": "Typed by hand.\n"})
    run_job(db, cfg, job_id,
            RunOptions(from_stage="preparing", until_stage="preparing", force_prepare=True))
    assert (cfg.jobs_dir / job_id / "spoken.txt").read_text() != "Typed by hand.\n"


# -- stopping and removing ---------------------------------------------------


def test_a_waiting_job_can_be_taken_out_of_the_queue(cfg, db, client):
    job_id = _ready(cfg, db)
    result = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert result["state"] == "cancelled"
    assert result["stopping"] is False
    assert db.claim_next_ready() is None
    # Its artifacts stay, so it can be put back.
    assert (cfg.jobs_dir / job_id / "spoken.txt").is_file()


def test_a_cancelled_job_never_reaches_the_feed(cfg, db, client):
    from readcast import feed as feed_mod

    job_id = _ready(cfg, db)
    client.post(f"/api/jobs/{job_id}/cancel")
    feed_mod.write_feed(db, cfg)
    xml = (cfg.feed_dir / "feed.xml").read_text()
    assert job_id not in xml


def test_a_running_render_is_asked_to_stop(cfg, db, client):
    job_id = _ready(cfg, db)
    db.update_job(job_id, state="synthesizing")
    result = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert result["stopping"] is True
    assert db.get_job(job_id)["cancel_requested"] == 1
    # The state is the worker's to change, once it reaches a chunk boundary.
    assert db.get_job(job_id)["state"] == "synthesizing"


def test_a_finished_job_cannot_be_cancelled(cfg, db, client):
    from readcast.worker import RunOptions, run_job

    job_id = _ready(cfg, db)
    run_job(db, cfg, job_id, RunOptions(from_stage="synthesizing"))
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_removing_a_job(cfg, db, client):
    job_id = _ready(cfg, db)
    assert client.delete(f"/api/jobs/{job_id}").json()["removed"] is True
    assert db.get_job(job_id) is None
    # Files are kept unless asked for.
    assert (cfg.jobs_dir / job_id).is_dir()

    other = _ready(cfg, db, "https://example.org/other")
    client.delete(f"/api/jobs/{other}?files=true")
    assert not (cfg.jobs_dir / other).exists()


def test_a_render_in_progress_must_be_stopped_before_removal(cfg, db, client):
    job_id = _ready(cfg, db)
    db.update_job(job_id, state="synthesizing")
    response = client.delete(f"/api/jobs/{job_id}")
    assert response.status_code == 409
    assert "stop the render first" in response.json()["detail"]
    assert db.get_job(job_id) is not None


def test_the_dashboard_offers_stop_and_remove(client):
    page = client.get("/ui").text
    assert "data-cancel=" in page
    assert "/cancel" in page
    assert 'id="stopnow"' in page
    assert "confirm(" in page      # stopping a render asks first
