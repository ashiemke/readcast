"""Feed rules from section 11, and acceptance tests 4, 6 and 8."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from readcast import feed as feed_mod
from readcast.db import new_job_id
from readcast.worker import run_job
from tests.test_pipeline import long_article_html, submit

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def parse(path):
    return ET.fromstring(path.read_bytes())


def channel(path):
    return parse(path).find("channel")


@pytest.fixture
def one_episode(cfg, db):
    job_id = submit(cfg, db, long_article_html(300), "https://example.org/one")
    run_job(db, cfg, job_id)
    return job_id


def test_rebuild_is_byte_identical(cfg, db, one_episode):
    """Acceptance test 4."""
    path = cfg.feed_dir / "feed.xml"
    first = path.read_bytes()
    path.unlink()
    feed_mod.write_feed(db, cfg)
    assert path.read_bytes() == first
    feed_mod.write_feed(db, cfg)
    assert path.read_bytes() == first


def test_enclosure_length_matches_the_file_on_disk(cfg, db, one_episode):
    """Acceptance test 8."""
    item = channel(cfg.feed_dir / "feed.xml").find("item")
    enclosure = item.find("enclosure")
    on_disk = feed_mod.episode_path(cfg.data_dir, one_episode).stat().st_size
    assert int(enclosure.get("length")) == on_disk
    assert enclosure.get("type") == "audio/mpeg"


def test_guid_is_the_job_id_and_is_not_a_permalink(cfg, db, one_episode):
    item = channel(cfg.feed_dir / "feed.xml").find("item")
    guid = item.find("guid")
    assert guid.text == one_episode
    assert guid.get("isPermaLink") == "false"


def test_pubdate_is_the_submission_time_not_the_article_date(cfg, db, one_episode):
    item = channel(cfg.feed_dir / "feed.xml").find("item")
    job = db.get_job(one_episode)
    parsed = feed_mod._parse_dt(item.find("pubDate").text)
    assert parsed.date() == feed_mod._parse_dt(job["submitted_at"]).date()
    # The article's own publish date is older; the feed must not use it.
    assert parsed.date() != feed_mod._parse_dt("2026-03-01").date()


def test_channel_blocks_directories_and_carries_itunes_duration(cfg, db, one_episode):
    ch = channel(cfg.feed_dir / "feed.xml")
    assert ch.find(f"{ITUNES}block").text.lower() in ("yes", "true")
    duration = ch.find("item").find(f"{ITUNES}duration").text
    assert int(duration) > 0
    assert int(duration) == int(round(db.get_job(one_episode)["duration_s"]))


def test_a_failed_job_never_enters_the_feed(cfg, db, one_episode):
    """Acceptance test 6."""
    bad = submit(cfg, db, "<html><body><p>short</p></body></html>", "https://example.org/bad")
    with pytest.raises(Exception):
        run_job(db, cfg, bad)
    feed_mod.write_feed(db, cfg)
    ids = [i.find("guid").text for i in channel(cfg.feed_dir / "feed.xml").findall("item")]
    assert one_episode in ids
    assert bad not in ids


def test_description_carries_source_author_and_flag_warning(cfg, db, one_episode):
    db.update_job(one_episode, flagged_chunks=2, author="A Writer", word_count=1234)
    feed_mod.write_feed(db, cfg)
    description = channel(cfg.feed_dir / "feed.xml").find("item").find("description").text
    assert "https://example.org/one" in description
    assert "A Writer" in description
    assert "1,234 words" in description
    assert "2 chunk(s) failed speech verification" in description


def test_max_items_trims_the_feed_but_not_the_disk(cfg, db, one_episode):
    for i in range(3):
        db.create_job(
            id=new_job_id(), url=f"https://example.org/{i}", state="done",
            title=f"Episode {i}", duration_s=10, bytes=100,
        )
    cfg.raw["feed"]["max_items"] = 2
    feed_mod.write_feed(db, cfg)
    items = channel(cfg.feed_dir / "feed.xml").findall("item")
    assert len(items) == 2
    assert feed_mod.episode_path(cfg.data_dir, one_episode).is_file()


def test_newest_first(cfg, db, one_episode):
    db.create_job(
        id="2099-01-01-zzzzzz", url="https://example.org/new", state="done",
        title="Newest", duration_s=5, bytes=10, submitted_at="2099-01-01T00:00:00+00:00",
    )
    feed_mod.write_feed(db, cfg)
    items = channel(cfg.feed_dir / "feed.xml").findall("item")
    assert items[0].find("guid").text == "2099-01-01-zzzzzz"
