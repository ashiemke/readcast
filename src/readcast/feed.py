"""Stage 7: publish.

The whole file is regenerated after every successful job. Patching an RSS file
in place is how feeds drift out of sync with what is on disk.

The feed URL is the credential: most podcast apps cannot send an
Authorization header, so the 32-character token in the path is the only gate.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator

log = logging.getLogger("readcast.feed")

GENERATOR = "readcast"


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = date_parser.parse(str(value))
        except (ValueError, OverflowError, TypeError):
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def audio_url(base_url: str, token: str, job: dict[str, Any]) -> str:
    version = int(job.get("render_version") or 1)
    return f"{base_url.rstrip('/')}/f/{token}/audio/{job['id']}.mp3?v={version}"


def feed_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/f/{token}/feed.xml"


def episode_description(job: dict[str, Any]) -> str:
    parts = [f'<p><a href="{html.escape(str(job.get("url") or ""))}">'
             f'{html.escape(str(job.get("url") or "source"))}</a></p>']
    if job.get("author"):
        parts.append(f"<p>By {html.escape(str(job['author']))}</p>")
    if job.get("word_count"):
        parts.append(f"<p>About {int(job['word_count']):,} words.</p>")
    flagged = int(job.get("flagged_chunks") or 0)
    if flagged:
        parts.append(
            f"<p><strong>{flagged} chunk(s) failed speech verification and may "
            "contain dropped words.</strong></p>"
        )
    return "".join(parts)


def build_feed(
    jobs: list[dict[str, Any]],
    *,
    base_url: str,
    token: str,
    feed_cfg: dict[str, Any],
    audio_dir: str | Path | None = None,
) -> bytes:
    fg = FeedGenerator()
    fg.load_extension("podcast")

    title = str(feed_cfg.get("title") or "readcast")
    author = str(feed_cfg.get("author") or "")
    fg.title(title)
    fg.link(href=feed_url(base_url, token), rel="self")
    fg.description(str(feed_cfg.get("description") or "Articles, read aloud."))
    fg.language(str(feed_cfg.get("language") or "en-us"))
    fg.id(feed_url(base_url, token))
    fg.generator(GENERATOR)
    fg.docs("")
    if author:
        fg.author({"name": author})
        fg.podcast.itunes_author(author)
    fg.podcast.itunes_block(True)
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_summary(str(feed_cfg.get("description") or "Articles, read aloud."))
    fg.image(url=f"{base_url.rstrip('/')}/f/{token}/cover.jpg", title=title,
             link=feed_url(base_url, token))
    fg.podcast.itunes_image(f"{base_url.rstrip('/')}/f/{token}/cover.jpg")

    max_items = int(feed_cfg.get("max_items", 100))
    ordered = sorted(jobs, key=lambda j: (str(j.get("submitted_at")), str(j["id"])), reverse=True)
    ordered = ordered[:max_items]

    newest = _parse_dt(ordered[0]["submitted_at"]) if ordered else datetime(
        1970, 1, 1, tzinfo=timezone.utc
    )
    # Deterministic: a rebuild with the same rows produces the same bytes.
    fg.lastBuildDate(newest)

    for job in reversed(ordered):  # feedgen prepends, so add oldest first
        entry = fg.add_entry()
        entry.id(str(job["id"]))
        entry.guid(str(job["id"]), permalink=False)
        entry.title(str(job.get("title") or job["id"]))
        entry.link(href=str(job.get("url") or base_url))
        entry.description(episode_description(job))
        entry.pubDate(_parse_dt(job["submitted_at"]))
        if job.get("author"):
            entry.podcast.itunes_author(str(job["author"]))

        size = int(job.get("bytes") or 0)
        if audio_dir:
            on_disk = Path(audio_dir) / f"{job['id']}.mp3"
            if on_disk.is_file():
                size = on_disk.stat().st_size
        entry.enclosure(
            url=audio_url(base_url, token, job), length=str(size), type="audio/mpeg"
        )
        duration = int(round(float(job.get("duration_s") or 0)))
        entry.podcast.itunes_duration(str(duration))
        entry.podcast.itunes_explicit("no")

    return fg.rss_str(pretty=True)


def episode_path(data_dir: str | Path, job_id: str) -> Path:
    return Path(data_dir) / "jobs" / job_id / "episode.mp3"


def publishable_jobs(db: Any) -> list[dict[str, Any]]:
    """A failed job never enters the feed."""
    return [j for j in db.list_jobs(state="done", limit=10000) if int(j.get("in_feed", 1))]


def write_feed(
    db: Any,
    cfg: Any,
    *,
    path: str | Path | None = None,
) -> Path:
    cfg.ensure_dirs()
    token = db.feed_token()
    jobs = publishable_jobs(db)
    for job in jobs:
        mp3 = episode_path(cfg.data_dir, str(job["id"]))
        if mp3.is_file():
            job["bytes"] = mp3.stat().st_size
    xml = build_feed(
        jobs,
        base_url=cfg.base_url,
        token=token,
        feed_cfg=cfg["feed"],
        audio_dir=None,
    )
    out = Path(path) if path else cfg.feed_dir / "feed.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(xml)
    log.info("wrote %s with %d item(s)", out, len(jobs))
    return out
