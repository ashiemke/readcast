"""The HTTP surface, and acceptance tests 7, 9 and 10."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from readcast import feed as feed_mod
from readcast.worker import RunOptions, run_job
from tests.test_pipeline import long_article_html, submit


def test_submit_requires_a_bearer_token(cfg, db, client):
    client.headers.pop("authorization")
    response = client.post("/jobs", json={"url": "https://example.org/x"})
    assert response.status_code == 401


def test_submit_queues_a_job_and_stores_client_html(cfg, db, client):
    html = long_article_html(200)
    response = client.post(
        "/jobs",
        json={"url": "https://example.org/x", "title": "From the browser", "client_html": html},
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    job = db.get_job(job_id)
    assert job["state"] == "queued"
    assert job["client_html"] == 1
    assert job["title"] == "From the browser"
    directory = cfg.jobs_dir / job_id
    assert (directory / "raw.html").read_text() == html
    assert (directory / "job.json").is_file()


def test_config_defaults_are_not_shared_between_instances(tmp_path):
    """A nested key absent from config.yml must not alias the module defaults."""
    from readcast.config import DEFAULTS, load_config

    (tmp_path / "config.yml").write_text("base_url: https://a.test\n")
    first = load_config(tmp_path / "config.yml")
    first.raw["fetch"]["max_client_html_bytes"] = 7
    second = load_config(tmp_path / "config.yml")
    assert second.raw["fetch"]["max_client_html_bytes"] != 7
    assert DEFAULTS["fetch"]["max_client_html_bytes"] != 7


def test_client_html_over_the_cap_is_rejected(cfg, db, client):
    cfg.raw["fetch"]["max_client_html_bytes"] = 1024
    response = client.post(
        "/jobs", json={"url": "https://example.org/x", "client_html": "x" * 2048}
    )
    assert response.status_code == 413


def test_cors_preflight_is_answered_for_the_bookmarklet(client):
    response = client.options(
        "/jobs",
        headers={
            "origin": "https://some-news-site.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_job_detail_includes_stage_timings(cfg, db, client):
    job_id = submit(cfg, db, long_article_html(200))
    run_job(db, cfg, job_id)
    body = client.get(f"/jobs/{job_id}").json()
    assert body["state"] == "done"
    assert body["stage_times"]["preparing"] >= 0
    assert client.get("/jobs?state=done").json()[0]["id"] == job_id
    assert client.get("/jobs/nope").status_code == 404


def test_status_page_lists_jobs(cfg, db, client):
    job_id = submit(cfg, db, long_article_html(200))
    run_job(db, cfg, job_id)
    response = client.get("/status")
    assert response.status_code == 200
    assert job_id in response.text
    assert response.headers["x-robots-tag"].startswith("noindex")


def test_rerender_keeps_the_id_and_guid_and_changes_the_enclosure_version(cfg, db, client):
    """Acceptance test 7."""
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    token = db.feed_token()
    before = ET.fromstring((cfg.feed_dir / "feed.xml").read_bytes())
    first_url = before.find("channel/item/enclosure").get("url")

    response = client.post(f"/jobs/{job_id}/rerender?from=preparing")
    assert response.status_code == 200
    job = db.get_job(job_id)
    assert job["state"] == "queued"
    assert job["pending_from"] == "preparing"
    assert job["render_version"] == 2

    run_job(db, cfg, job_id, RunOptions(from_stage="preparing"))
    after = ET.fromstring((cfg.feed_dir / "feed.xml").read_bytes())
    item = after.find("channel/item")
    assert item.find("guid").text == job_id
    second_url = item.find("enclosure").get("url")
    assert first_url != second_url
    assert second_url.endswith("?v=2")
    assert f"/f/{token}/audio/{job_id}.mp3" in second_url


def test_rerender_rejects_an_unknown_stage(cfg, db, client):
    job_id = submit(cfg, db, long_article_html(200))
    assert client.post(f"/jobs/{job_id}/rerender?from=nonsense").status_code == 400


def test_delete_removes_from_the_feed_but_keeps_the_files(cfg, db, client):
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    episode = feed_mod.episode_path(cfg.data_dir, job_id)
    assert client.delete(f"/jobs/{job_id}").status_code == 200
    assert episode.is_file()
    feed = ET.fromstring((cfg.feed_dir / "feed.xml").read_bytes())
    assert feed.find("channel/item") is None
    token = db.feed_token()
    assert client.get(f"/f/{token}/audio/{job_id}.mp3").status_code == 404


def test_feed_and_audio_are_served_under_the_token(cfg, db, client):
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    token = db.feed_token()

    feed = client.get(f"/f/{token}/feed.xml")
    assert feed.status_code == 200
    assert feed.headers["content-type"].startswith("application/rss+xml")
    assert feed.headers["x-robots-tag"].startswith("noindex")

    audio = client.get(f"/f/{token}/audio/{job_id}.mp3")
    assert audio.status_code == 200
    assert audio.headers["accept-ranges"] == "bytes"
    assert audio.headers["x-robots-tag"].startswith("noindex")
    assert int(audio.headers["content-length"]) == feed_mod.episode_path(
        cfg.data_dir, job_id
    ).stat().st_size


def test_range_request_returns_206(cfg, db, client):
    """Acceptance test 9."""
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    token = db.feed_token()
    response = client.get(
        f"/f/{token}/audio/{job_id}.mp3", headers={"range": "bytes=100-200"}
    )
    assert response.status_code == 206
    assert len(response.content) == 101
    size = feed_mod.episode_path(cfg.data_dir, job_id).stat().st_size
    assert response.headers["content-range"] == f"bytes 100-200/{size}"
    assert response.headers["content-length"] == "101"

    whole = client.get(f"/f/{token}/audio/{job_id}.mp3").content
    assert response.content == whole[100:201]

    beyond = client.get(
        f"/f/{token}/audio/{job_id}.mp3", headers={"range": f"bytes={size + 10}-"}
    )
    assert beyond.status_code == 416


def test_rotating_the_token_makes_the_old_url_404(cfg, db, client):
    """Acceptance test 10."""
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    old = db.feed_token()
    assert client.get(f"/f/{old}/feed.xml").status_code == 200

    new = db.feed_token(rotate=True)
    feed_mod.write_feed(db, cfg)
    assert new != old
    assert client.get(f"/f/{old}/feed.xml").status_code == 404
    assert client.get(f"/f/{old}/audio/{job_id}.mp3").status_code == 404
    assert client.get(f"/f/{new}/feed.xml").status_code == 200


def test_a_default_api_token_refuses_work(cfg, db):
    from fastapi.testclient import TestClient

    from readcast.api import create_app

    cfg.raw["api_token"] = "CHANGE_ME"
    app = create_app(cfg, start_worker=False, db=db)
    with TestClient(app) as unsafe:
        response = unsafe.post(
            "/jobs",
            json={"url": "https://example.org/x"},
            headers={"authorization": "Bearer CHANGE_ME"},
        )
    assert response.status_code == 503


def test_head_requests_are_answered(cfg, db, client):
    """Podcast clients HEAD the enclosure before downloading it."""
    job_id = submit(cfg, db, long_article_html(300))
    run_job(db, cfg, job_id)
    token = db.feed_token()
    size = feed_mod.episode_path(cfg.data_dir, job_id).stat().st_size

    head = client.head(f"/f/{token}/audio/{job_id}.mp3")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(size)
    assert head.headers["accept-ranges"] == "bytes"
    assert head.headers["content-type"] == "audio/mpeg"
    assert head.content == b""

    assert client.head(f"/f/{token}/feed.xml").status_code == 200
    assert client.head(f"/f/wrong-token/feed.xml").status_code == 404


def test_form_submit_path_for_pages_that_block_fetch(cfg, db, client):
    """Wikipedia's CSP blocks a bookmarklet's fetch; the form POST must work."""
    html = long_article_html(200)
    response = client.post(
        "/jobs/form",
        data={"url": "https://example.org/x", "title": "Via form",
              "client_html": html, "token": "test-token"},
    )
    assert response.status_code == 200
    assert "queued" in response.text.lower()
    assert response.headers["content-type"].startswith("text/html")

    job = db.list_jobs(limit=1)[0]
    assert job["title"] == "Via form"
    assert job["client_html"] == 1
    assert (cfg.jobs_dir / job["id"] / "raw.html").read_text() == html
    assert job["id"] in response.text


def test_form_submit_rejects_a_bad_token(cfg, db, client):
    response = client.post(
        "/jobs/form", data={"url": "https://example.org/x", "token": "wrong"}
    )
    assert response.status_code == 401
    assert not db.list_jobs(limit=1)


def test_form_submit_rejects_an_oversized_page(cfg, db, client):
    cfg.raw["fetch"]["max_client_html_bytes"] = 1024
    response = client.post(
        "/jobs/form",
        data={"url": "https://example.org/x", "token": "test-token",
              "client_html": "x" * 2048},
    )
    assert response.status_code == 413
    assert not db.list_jobs(limit=1)
