"""`readcast watch` — a live view of what the worker is doing.

Reads the database and the job directories. It never writes anything, so it is
safe to run alongside the worker, or several times over.
"""

from __future__ import annotations

import json
import select
import socket
import sys
import termios
import time
import tty
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from readcast import feed as feed_mod
from readcast.config import Config
from readcast.db import Database

RUNNING = ("synthesizing", "assembling", "fetching", "extracting", "preparing")

STATE_STYLE = {
    "done": "green",
    "failed": "red",
    "queued": "grey62",
    "ready": "green3",
    "fetching": "yellow",
    "extracting": "yellow",
    "preparing": "yellow",
    "synthesizing": "cyan",
    "assembling": "yellow",
}


def _hms(seconds: float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def _clock(seconds: float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _url_parts(url: str, default_port: int) -> tuple[str, int]:
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = parts.hostname or "127.0.0.1"
    return ("127.0.0.1" if host == "0.0.0.0" else host, parts.port or default_port)


# Measured on an M-series Mac. The learned value in the database overrides these
# as soon as one job has run on that backend. Backends differ by two orders of
# magnitude, so one shared number would be wrong for whichever is not in use.
DEFAULT_SECONDS_PER_CHUNK = 50.0
BACKEND_SECONDS_PER_CHUNK = {
    "mlx": 50.0,        # autoregressive, ~0.3x real time
    "kokoro": 1.0,      # non-autoregressive, ~30x real time
    "elevenlabs": 2.0,
    "openai": 2.0,
    "test": 0.1,
}
EWMA_ALPHA = 0.3
REUSE_FLOOR_S = 1.0      # anything faster than this was reused, not synthesized


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    started: float | None = None
    per_chunk: float | None = None
    fresh: int = 0           # chunks actually synthesized in this run

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 0.0

    @property
    def eta_s(self) -> float | None:
        if not (self.per_chunk and self.total):
            return None
        return max(self.total - self.done, 0) * self.per_chunk

    @property
    def finish_at(self) -> float | None:
        eta = self.eta_s
        return time.time() + eta if eta is not None else None


def blend_rate(
    gaps: list[float], prior: float, alpha: float = EWMA_ALPHA
) -> float:
    """A decaying average of recent chunks, seeded with what we already know.

    Outliers are clamped rather than dropped: a retry or a paused machine should
    nudge the estimate, not redefine it.
    """
    rate = prior
    for gap in gaps:
        if gap < REUSE_FLOOR_S:
            continue          # a reused chunk costs no time and proves nothing
        rate = alpha * min(gap, prior * 6) + (1 - alpha) * rate
    return rate


# A worker writes its position every chunk; anything older than this is from a
# run that has since stopped, so fall back to reading the directory.
PROGRESS_STALE_S = 15 * 60


def chunk_progress(job_dir: Path, prior: float = DEFAULT_SECONDS_PER_CHUNK) -> Progress:
    """Live progress, from the worker's own report where there is one.

    Counting chunk files is only a fallback: a rerender overwrites 000.wav
    upward, so the file count stays flat while the worker is halfway through,
    and reused chunks are hardlinks carrying the mtime of an older render.
    """
    plan = job_dir / "chunks" / "plan.jsonl"
    if not plan.is_file():
        return Progress()
    total = sum(1 for line in plan.read_text().splitlines() if line.strip())
    started = plan.stat().st_mtime

    reported = job_dir / "chunks" / "progress.json"
    if reported.is_file():
        try:
            data = json.loads(reported.read_text())
        except (ValueError, OSError):
            data = None
        if data and time.time() - float(data.get("updated", 0)) < PROGRESS_STALE_S:
            stamps = [float(t) for t in data.get("stamps") or []]
            gaps = [b - a for a, b in zip(stamps, stamps[1:])]
            return Progress(
                done=int(data.get("done", 0)),
                total=int(data.get("total", total)) or total,
                started=float(data.get("started", started)),
                per_chunk=blend_rate(gaps, prior),
                fresh=len(stamps),
            )

    stamps = sorted(
        w.stat().st_mtime for w in (job_dir / "chunks").glob("[0-9]*.wav")
    )
    done = len(stamps)
    fresh = [t for t in stamps if t >= started]
    gaps = [b - a for a, b in zip([started, *fresh], fresh)] if fresh else []
    return Progress(
        done=done,
        total=total,
        started=started,
        per_chunk=blend_rate(gaps, prior),
        fresh=len(fresh),
    )


def when(timestamp: float | None) -> str:
    """A clock time the operator can plan around, not just a duration."""
    if not timestamp:
        return "—"
    from datetime import datetime

    finish = datetime.fromtimestamp(timestamp)
    now = datetime.now()
    days = (finish.date() - now.date()).days
    if days == 0:
        return finish.strftime("%H:%M")
    if days == 1:
        return finish.strftime("tomorrow %H:%M")
    return finish.strftime("%a %H:%M")


def bar(fraction: float, width: int = 34, style: str = "cyan") -> Text:
    filled = int(round(fraction * width))
    text = Text()
    text.append("█" * filled, style=style)
    text.append("░" * (width - filled), style="grey30")
    return text


def _rate_key(backend: str | None) -> str:
    return f"seconds_per_chunk:{backend}" if backend else "seconds_per_chunk"


def _stored_rate(db: Database, key: str) -> float:
    try:
        return float(db.get_setting(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def seconds_per_chunk(
    db: Database, backend: str | None = None,
    fallback: float = DEFAULT_SECONDS_PER_CHUNK,
) -> float:
    """What this machine manages on this backend.

    Per backend, because they are two orders of magnitude apart: a shared
    average would give a six-hour estimate to a ten-minute render for as long
    as it took to converge.
    """
    value = _stored_rate(db, _rate_key(backend)) if backend else 0.0
    if value <= 0 and backend:
        value = BACKEND_SECONDS_PER_CHUNK.get(backend, 0.0)
    if value <= 0:
        value = _stored_rate(db, "seconds_per_chunk")
    return value if value > 0 else fallback


def record_chunk_rate(db: Database, seconds: float, chunks: int,
                      backend: str | None = None,
                      alpha: float = EWMA_ALPHA) -> float | None:
    """Fold one job's measured rate into the persisted estimate."""
    if chunks <= 0 or seconds <= 0:
        return None
    observed = seconds / chunks
    key = _rate_key(backend)
    previous = _stored_rate(db, key)
    blended = observed if previous <= 0 else alpha * observed + (1 - alpha) * previous
    db.set_setting(key, f"{blended:.4f}")
    return blended


def _header(cfg: Config, db: Database, services: dict[str, bool]) -> Panel:
    line = Text()
    for name, ok in services.items():
        line.append("● ", style="green" if ok else "red")
        line.append(f"{name}   ", style="white" if ok else "grey50")
    line.append("\n")
    line.append("feed  ", style="grey62")
    line.append(feed_mod.feed_url(cfg.base_url, db.feed_token()), style="cyan")
    return Panel(line, title="readcast", title_align="left", border_style="grey35")


def _now_panel(cfg: Config, db: Database, job: dict[str, Any] | None) -> Panel:
    if job is None:
        body: Any = Align.center(Text("idle — nothing running", style="grey54"), vertical="middle")
        return Panel(body, title="now", title_align="left", border_style="grey35", height=4)

    state = str(job["state"])
    lines = Text()
    lines.append(str(job.get("title") or job.get("url") or job["id"])[:70] + "\n", style="bold")
    lines.append(f"{state:<14}", style=STATE_STYLE.get(state, "white"))

    progress = chunk_progress(
        cfg.jobs_dir / str(job["id"]), seconds_per_chunk(db, job.get("backend"))
    )
    if state == "synthesizing" and progress.total:
        lines.append_text(bar(progress.fraction))
        lines.append(f"  {progress.done}/{progress.total} chunks\n")
        if progress.eta_s:
            lines.append("eta ", style="grey54")
            lines.append(_hms(progress.eta_s), style="grey62")
            lines.append("   done ", style="grey54")
            lines.append(when(progress.finish_at), style="green3")
        if progress.per_chunk:
            lines.append(f"   {progress.per_chunk:.0f}s/chunk", style="grey54")
        if progress.started:
            lines.append(f"   up {_hms(time.time() - progress.started)}", style="grey54")
    else:
        lines.append("working…", style="grey62")

    lines.append("\n")
    detail = Text(style="grey54")
    detail.append(f"{job['id']}   backend {job.get('backend') or '—'}")
    if job.get("voice_main"):
        detail.append(f"   voice {job['voice_main']}")
    if job.get("word_count"):
        detail.append(f"   {int(job['word_count']):,} words")
    if job.get("flagged_chunks"):
        detail.append(f"   {job['flagged_chunks']} flagged", style="yellow")
    lines.append_text(detail)
    return Panel(lines, title="now", title_align="left", border_style="cyan")


def _queue_panel(db: Database) -> Panel:
    # ready_queue() is the order the audio lane will actually claim in.
    ready = db.ready_queue()
    queued = db.list_jobs(state="queued", limit=50)
    body = Text()

    if ready:
        body.append("ready — text on disk, waiting for audio\n", style="green3")
        for job in ready:
            held = int(job.get("hold") or 0)
            body.append("  hold  " if held else "  next  ",
                        style="yellow" if held else "grey54")
            body.append(str(job.get("title") or job.get("url"))[:56], style="white")
            if job.get("word_count"):
                body.append(f"  {int(job['word_count']):,}w", style="grey54")
            body.append("\n")
    if queued:
        body.append("waiting to be prepared\n", style="grey62")
        for job in reversed(queued):
            body.append("  ····  ", style="grey54")
            body.append(str(job.get("title") or job.get("url"))[:56] + "\n", style="grey62")
    if not ready and not queued:
        body = Text("empty", style="grey54")

    return Panel(
        body,
        title=f"queue ({len(ready) + len(queued)})",
        title_align="left",
        border_style="grey35",
    )


def _recent_table(db: Database, limit: int = 10) -> Panel:
    # Fixed widths for the narrow columns; the title takes whatever is left.
    table = Table(box=None, expand=True, pad_edge=False, show_edge=False)
    table.add_column("", style="grey54", no_wrap=True, width=3)
    table.add_column("id", style="grey54", no_wrap=True, width=17)
    table.add_column("state", no_wrap=True, width=12)
    table.add_column("len", justify="right", no_wrap=True, width=5)
    table.add_column("rtf", justify="right", no_wrap=True, width=4)
    table.add_column("flag", justify="right", no_wrap=True, width=4)
    table.add_column("title", overflow="ellipsis", no_wrap=True, ratio=1)

    for i, job in enumerate(listed_jobs(db, limit), start=1):
        state = str(job["state"])
        flagged = int(job.get("flagged_chunks") or 0)
        table.add_row(
            f"{i}" if i <= 9 else "",
            str(job["id"]),
            Text(state, style=STATE_STYLE.get(state, "white")),
            _clock(job.get("duration_s")),
            f"{job['rtf']:.2f}" if job.get("rtf") else "—",
            Text(str(flagged), style="yellow") if flagged else "",
            Text(
                str(job.get("title") or job.get("url") or ""),
                style="red" if state == "failed" else "white",
            ),
        )
    return Panel(table, title="recent", title_align="left", border_style="grey35")


# -- inspecting one job -----------------------------------------------------


def job_artifacts(cfg: Config, job: dict[str, Any]) -> dict[str, Path]:
    d = cfg.jobs_dir / str(job["id"])
    return {
        "spoken": d / "spoken.txt",
        "extracted": d / "extracted.md",
        "plan": d / "chunks" / "plan.jsonl",
        "transforms": d / "transforms.jsonl",
        "unknowns": d / "unknowns.jsonl",
    }


def job_detail(cfg: Config, db: Database, job: dict[str, Any], *, chunks: bool = False) -> Group:
    """What the engine is given, for one job."""
    files = job_artifacts(cfg, job)
    state = str(job["state"])

    head = Text()
    head.append(str(job.get("title") or job.get("url") or job["id"]) + "\n", style="bold")
    head.append(str(job.get("url") or "") + "\n", style="cyan")
    head.append(f"{job['id']}   ", style="grey54")
    head.append(state, style=STATE_STYLE.get(state, "white"))
    if job.get("word_count"):
        head.append(f"   {int(job['word_count']):,} words", style="grey54")
    if job.get("backend"):
        head.append(f"   backend {job['backend']}", style="grey54")
    if job.get("voice_main"):
        head.append(f"   voice {job['voice_main']}", style="grey54")
    times = db.stage_times(str(job["id"]))
    if times:
        head.append("\n" + "  ".join(f"{k} {_hms(v)}" for k, v in times.items()), style="grey54")

    body: Any
    if chunks and files["plan"].is_file():
        table = Table(box=None, expand=True, pad_edge=False)
        table.add_column("#", style="grey54", width=4, justify="right")
        table.add_column("kind", width=8)
        table.add_column("chars", width=5, justify="right")
        table.add_column("text", ratio=1, overflow="fold")
        for line in files["plan"].read_text().splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            table.add_row(
                str(c["index"]),
                Text(c["kind"], style="cyan" if c["kind"] == "heading" else "grey62"),
                str(len(c["text"])),
                c["text"],
            )
        body = table
        title = "chunks — exactly what each synthesis call receives"
    elif files["spoken"].is_file():
        body = Text(files["spoken"].read_text())
        title = "spoken.txt — exactly what the speech engine is given"
    elif files["extracted"].is_file():
        body = Text(files["extracted"].read_text())
        title = "extracted.md — not prepared yet, so this is pre-rules text"
    else:
        note = Text()
        note.append("No text yet.\n\n", style="yellow")
        note.append(
            "A queued job has not been fetched or prepared. spoken.txt appears\n"
            "once the preparing stage runs. Until then there is nothing to show\n"
            "but the URL above.",
            style="grey62",
        )
        body = note
        title = "no text yet"

    return Group(
        Panel(head, border_style="cyan"),
        Panel(body, title=title, title_align="left", border_style="grey35"),
    )


# -- interaction ------------------------------------------------------------


@contextmanager
def _cbreak():
    """Read single keypresses without waiting for Enter."""
    if not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _key(timeout: float) -> str | None:
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if ready else None


def listed_jobs(db: Database, limit: int = 10) -> list[dict[str, Any]]:
    """The rows the dashboard shows, in the order it shows them."""
    return db.list_jobs(limit=limit)


def _footer(db: Database) -> Text:
    done = [j for j in db.list_jobs(state="done", limit=10_000) if int(j.get("in_feed", 1))]
    total_s = sum(float(j.get("duration_s") or 0) for j in done)
    text = Text(style="grey54")
    text.append(f"{len(done)} episode{'s' if len(done) != 1 else ''}")
    text.append(f" · {_hms(total_s)} of audio")
    failed = len(db.list_jobs(state="failed", limit=10_000))
    if failed:
        text.append(f" · {failed} failed", style="red")
    text.append("     1-9 view text · c chunks · q quit")
    return text


def render(cfg: Config, db: Database, services: dict[str, bool]) -> Group:
    running = None
    for state in RUNNING:
        found = db.list_jobs(state=state, limit=1)
        if found:
            running = found[0]
            break
    return Group(
        _header(cfg, db, services),
        _now_panel(cfg, db, running),
        _queue_panel(db),
        _recent_table(db),
        _footer(db),
    )


def check_services(cfg: Config) -> dict[str, bool]:
    api_host, api_port = _url_parts(cfg.base_url, 8000)
    tts_host, tts_port = _url_parts(str(cfg["tts"].get("mlx_url") or ""), 8080)
    return {
        f"api {api_port}": port_open(api_host, api_port),
        f"speech {tts_port}": port_open(tts_host, tts_port),
    }


def watch(cfg: Config, db: Database, *, interval: float = 1.0, once: bool = False) -> None:
    console = Console()
    if once:
        console.print(render(cfg, db, check_services(cfg)))
        return

    services = check_services(cfg)
    last_check = time.time()
    show_chunks = False

    with _cbreak() as interactive:
        live = Live(render(cfg, db, services), console=console, refresh_per_second=4)
        live.start()
        try:
            while True:
                key = _key(interval)
                if key in ("q", "Q", "\x03"):  # q or ctrl-c
                    break
                if key in ("c", "C"):
                    show_chunks = not show_chunks
                if key and key.isdigit() and key != "0":
                    jobs = listed_jobs(db)
                    index = int(key) - 1
                    if index < len(jobs):
                        # Leave the live view, page the text, come back.
                        live.stop()
                        _page(console, cfg, db, jobs[index], chunks=show_chunks)
                        live.start()
                if time.time() - last_check > 5:  # a TCP probe is not free
                    services = check_services(cfg)
                    last_check = time.time()
                live.update(render(cfg, db, services))
                if not interactive and not sys.stdin.isatty():
                    continue
        except KeyboardInterrupt:
            pass
        finally:
            live.stop()


def _page(console: Console, cfg: Config, db: Database, job: dict[str, Any],
          *, chunks: bool = False) -> None:
    """Show one job's text in the system pager, so long articles scroll."""
    fresh = db.get_job(str(job["id"])) or job
    detail = job_detail(cfg, db, fresh, chunks=chunks)
    saved = None
    if sys.stdin.isatty():  # the pager wants the terminal back in cooked mode
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    try:
        with console.pager(styles=True):
            console.print(detail)
    finally:
        if saved is not None:
            tty.setcbreak(sys.stdin.fileno())
