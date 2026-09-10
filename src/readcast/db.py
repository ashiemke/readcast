"""SQLite state. One file, WAL mode, one writer.

The job row is the whole state machine. Artifacts live on disk next to it, so a
failed job keeps everything it managed to write.
"""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATES = (
    "queued",       # submitted, nothing read yet
    "fetching",
    "extracting",
    "preparing",
    "ready",        # text on disk, waiting for the expensive part
    "synthesizing",
    "assembling",
    "done",
    "failed",
    "cancelled",    # stopped on purpose; artifacts kept, never published
)

# Text preparation is seconds and touches no model; synthesis holds several
# gigabytes. They are separate lanes with separate locks.
PREP_STAGES = ("fetching", "extracting", "preparing")
AUDIO_STAGES = ("synthesizing", "assembling")

# The stage a rerender may start from, in pipeline order.
STAGES = ("fetching", "extracting", "preparing", "synthesizing", "assembling")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    url            TEXT NOT NULL,
    title          TEXT,
    author         TEXT,
    publication    TEXT,
    published_at   TEXT,
    submitted_at   TEXT NOT NULL,
    state          TEXT NOT NULL,
    stage_failed   TEXT,
    error          TEXT,
    backend        TEXT,
    model          TEXT,
    voice          TEXT,
    duration_s     REAL,
    bytes          INTEGER,
    rtf            REAL,
    client_html    INTEGER NOT NULL DEFAULT 0,
    flagged_chunks INTEGER NOT NULL DEFAULT 0,
    word_count     INTEGER,
    render_version INTEGER NOT NULL DEFAULT 1,
    in_feed        INTEGER NOT NULL DEFAULT 1,
    pending_from   TEXT,
    hold           INTEGER NOT NULL DEFAULT 0,
    queue_rank     INTEGER,
    voice_main     TEXT,
    voice_quote    TEXT,
    voice_aside    TEXT,
    text_edited    INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    title_locked   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stage_times (
    job_id     TEXT NOT NULL,
    stage      TEXT NOT NULL,
    seconds    REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (job_id, stage)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_submitted_idx ON jobs(submitted_at);
"""


def utcnow() -> str:
    """Microsecond precision so the queue is genuinely first in, first out.

    At second precision, several clicks inside one second sorted by the random
    part of the job id instead of by arrival.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_job_id(when: datetime | None = None) -> str:
    """`YYYY-MM-DD-<6 char base32>`."""
    when = when or datetime.now(timezone.utc)
    suffix = base64.b32encode(secrets.token_bytes(4)).decode().rstrip("=").lower()[:6]
    return f"{when.strftime('%Y-%m-%d')}-{suffix}"


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does not
# touch an existing table, so an upgrade has to add them explicitly.
ADDED_COLUMNS = {
    "model": "TEXT",
    "word_count": "INTEGER",
    "render_version": "INTEGER NOT NULL DEFAULT 1",
    "in_feed": "INTEGER NOT NULL DEFAULT 1",
    "pending_from": "TEXT",
    "hold": "INTEGER NOT NULL DEFAULT 0",
    "queue_rank": "INTEGER",
    "voice_main": "TEXT",
    "voice_quote": "TEXT",
    "voice_aside": "TEXT",
    "text_edited": "INTEGER NOT NULL DEFAULT 0",
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
    "title_locked": "INTEGER NOT NULL DEFAULT 0",
}


class Database:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        present = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in ADDED_COLUMNS.items():
            if name not in present:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # -- jobs ---------------------------------------------------------------

    def create_job(self, **fields: Any) -> str:
        fields.setdefault("id", new_job_id())
        fields.setdefault("submitted_at", utcnow())
        fields.setdefault("state", "queued")
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.connect() as conn:
            conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
        return str(fields["id"])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id)
            )

    def list_jobs(
        self, state: str | None = None, limit: int = 100, in_feed: bool | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        where, params = [], []
        if state:
            where.append("state = ?")
            params.append(state)
        if in_feed is not None:
            where.append("in_feed = ?")
            params.append(int(in_feed))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY submitted_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _claim(self, from_state: str, to_state: str, extra: str = "") -> dict[str, Any] | None:
        """Take the oldest job in a state. The UPDATE is the lock."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM jobs WHERE state = ? {extra}"
                " ORDER BY (queue_rank IS NULL), queue_rank, submitted_at, id"
                " LIMIT 1",
                (from_state,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE jobs SET state = ?, error = NULL, stage_failed = NULL"
                " WHERE id = ? AND state = ?",
                (to_state, row["id"], from_state),
            )
            conn.execute("COMMIT")
            return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())

    def claim_next_queued(self) -> dict[str, Any] | None:
        """For the prep lane: a freshly submitted job."""
        return self._claim("queued", "fetching")

    def claim_next_ready(self) -> dict[str, Any] | None:
        """For the audio lane: prepared text that is not being held back."""
        return self._claim("ready", "synthesizing", extra="AND hold = 0")

    def ready_queue(self) -> list[dict[str, Any]]:
        """Jobs waiting for the audio lane, in the order they will run."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'ready'"
                " ORDER BY (queue_rank IS NULL), queue_rank, submitted_at, id"
            ).fetchall()
        return [dict(r) for r in rows]

    def next_queue_rank(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(queue_rank), 0) AS top FROM jobs"
            ).fetchone()
        return int(row["top"]) + 1

    def set_queue_order(self, job_ids: list[str]) -> list[str]:
        """Assign explicit ranks. Anything not named keeps its arrival order."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for rank, job_id in enumerate(job_ids, start=1):
                conn.execute(
                    "UPDATE jobs SET queue_rank = ? WHERE id = ? AND state = 'ready'",
                    (rank, job_id),
                )
            conn.execute("COMMIT")
        return [j["id"] for j in self.ready_queue()]

    def delete_job(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.execute("DELETE FROM stage_times WHERE job_id = ?", (job_id,))

    # -- stage timings ------------------------------------------------------

    def record_stage(self, job_id: str, stage: str, seconds: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO stage_times (job_id, stage, seconds, recorded_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(job_id, stage) DO UPDATE SET seconds = excluded.seconds,"
                " recorded_at = excluded.recorded_at",
                (job_id, stage, round(seconds, 3), utcnow()),
            )

    def stage_times(self, job_id: str) -> dict[str, float]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT stage, seconds FROM stage_times WHERE job_id = ?", (job_id,)
            ).fetchall()
        return {r["stage"]: r["seconds"] for r in rows}

    # -- settings -----------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def feed_token(self, rotate: bool = False) -> str:
        """The feed URL is the credential. 32 URL-safe characters."""
        token = None if rotate else self.get_setting("feed_token")
        if not token:
            token = secrets.token_urlsafe(24)[:32]
            self.set_setting("feed_token", token)
        return token


class stage_timer:
    """Context manager that records the wall time of one stage."""

    def __init__(self, db: Database, job_id: str, stage: str):
        self.db, self.job_id, self.stage = db, job_id, stage

    def __enter__(self) -> "stage_timer":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.elapsed = time.perf_counter() - self.t0
        self.db.record_stage(self.job_id, self.stage, self.elapsed)
