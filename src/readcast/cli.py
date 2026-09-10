"""The command line. The tuning loop lives here.

The operator hears a wrong pronunciation, finds the term with `lexicon
suggest`, tests a respelling with `say`, commits the rule, and runs `rerender`
on the affected episode.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer

from readcast import feed as feed_mod
from readcast.config import Config, load_config
from readcast.db import STAGES, Database, new_job_id
from readcast.worker import (
    PrepWorker, RunOptions, Worker, job_dir, run_job, write_job_json,
)

app = typer.Typer(add_completion=False, help="readcast — articles, read aloud.")
rules_app = typer.Typer(help="Rule tests.")
lexicon_app = typer.Typer(help="Lexicon work queue.")
feed_app = typer.Typer(help="Feed maintenance.")
voices_app = typer.Typer(help="The reading voices.")
app.add_typer(rules_app, name="rules")
app.add_typer(lexicon_app, name="lexicon")
app.add_typer(feed_app, name="feed")
app.add_typer(voices_app, name="voices")

CONFIG_OPTION = typer.Option(None, "--config", "-c", help="Path to config.yml.")


def _setup(config: str | None, verbose: bool = False) -> tuple[Config, Database]:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    cfg = load_config(config)
    cfg.ensure_dirs()
    return cfg, Database(cfg.db_path)


def job_url(db: Database, job_id: str) -> str:
    job = db.get_job(job_id) or {}
    return str(job.get("url") or "")


def _is_pdf_shell(path: Path, url: str) -> bool:
    from readcast.extract import looks_like_pdf

    try:
        return looks_like_pdf(path.read_text(errors="replace"), url)
    except OSError:
        return True


def echo(message: str = "") -> None:
    typer.echo(message)


def _play(path: Path) -> None:
    player = next((p for p in ("afplay", "ffplay", "aplay") if shutil.which(p)), None)
    if not player:
        echo(f"  (no audio player found; the file is at {path})")
        return
    args = [player, str(path)]
    if player == "ffplay":
        args = ["ffplay", "-autoexit", "-nodisp", "-loglevel", "error", str(path)]
    subprocess.run(args, check=False)


# -- serve ------------------------------------------------------------------


@app.command()
def serve(
    config: str | None = CONFIG_OPTION,
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, help="Bind port."),
    reload: bool = typer.Option(False, help="Reload on source changes."),
) -> None:
    """Run the API and the single worker."""
    import uvicorn

    from readcast.api import create_app

    cfg, db = _setup(config)
    if config:
        os.environ["READCAST_CONFIG"] = str(Path(config).resolve())
    echo(f"readcast on http://{host}:{port}")
    echo(f"feed: {feed_mod.feed_url(cfg.base_url, db.feed_token())}")
    if cfg.api_token == "CHANGE_ME":
        echo("warning: api_token is still CHANGE_ME; POST /jobs will refuse work")
    uvicorn.run(
        "readcast.api:get_app" if reload else create_app(cfg),
        host=host,
        port=port,
        reload=reload,
        factory=reload,
        log_level="info",
    )


# -- add / jobs -------------------------------------------------------------


@app.command()
def add(
    url: str,
    config: str | None = CONFIG_OPTION,
    backend: str | None = typer.Option(None, "--backend", "-b"),
    voice: str | None = typer.Option(None, "--voice", "-v"),
    queue_only: bool = typer.Option(False, help="Queue without running it here."),
) -> None:
    """Queue a URL. Runs it here when no other worker holds the lock."""
    cfg, db = _setup(config)
    job_id = db.create_job(
        id=new_job_id(), url=url, backend=backend, voice=voice
    )
    write_job_json(job_dir(cfg, job_id), {"id": job_id, "url": url})
    echo(f"queued {job_id}")
    if queue_only:
        return
    worker = Worker(db, cfg)
    if not worker._acquire_lock():  # noqa: SLF001 - the lock is the point
        echo("a worker is already running; it will pick this up")
        return
    try:
        worker.run_once()
    finally:
        worker.stop()
    job = db.get_job(job_id) or {}
    if job.get("state") == "done":
        echo(f"done: {feed_mod.episode_path(cfg.data_dir, job_id)}")
    else:
        echo(f"{job.get('state')}: {job.get('error') or ''}")
        raise typer.Exit(code=1)


@app.command()
def watch(
    config: str | None = CONFIG_OPTION,
    interval: float = typer.Option(1.0, "--interval", "-i", help="Seconds between refreshes."),
    once: bool = typer.Option(False, "--once", help="Print one snapshot and exit."),
) -> None:
    """Live dashboard: what the worker is doing right now."""
    from readcast.dashboard import watch as run_watch

    cfg = load_config(config)
    cfg.ensure_dirs()
    run_watch(cfg, Database(cfg.db_path), interval=interval, once=once)


@app.command()
def jobs(
    config: str | None = CONFIG_OPTION,
    state: str | None = typer.Option(None, "--state", "-s"),
    limit: int = typer.Option(20, "--limit", "-n"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List jobs."""
    _, db = _setup(config)
    rows = db.list_jobs(state=state, limit=limit)
    if as_json:
        echo(json.dumps(rows, indent=2))
        return
    if not rows:
        echo("no jobs")
        return
    echo(f"{'id':22} {'state':13} {'rtf':>5} {'len':>7}  title")
    for job in rows:
        length = float(job.get("duration_s") or 0)
        echo(
            f"{job['id']:22} {str(job['state']):13} "
            f"{(f'{job['rtf']:.2f}' if job.get('rtf') else ''):>5} "
            f"{(f'{int(length)//60}:{int(length)%60:02d}' if length else ''):>7}  "
            f"{str(job.get('title') or job.get('url') or '')[:60]}"
        )
        if job.get("error"):
            echo(f"{'':22} {job['stage_failed']}: {job['error'][:90]}")


@app.command()
def rerender(
    job_id: str,
    config: str | None = CONFIG_OPTION,
    from_stage: str = typer.Option("preparing", "--from", help=f"One of: {', '.join(STAGES)}"),
) -> None:
    """Re-run a job from a stage. Same id, same guid, new enclosure version."""
    cfg, db = _setup(config)
    job = db.get_job(job_id)
    if job is None:
        echo(f"no such job: {job_id}")
        raise typer.Exit(code=1)
    if from_stage not in STAGES:
        echo(f"unknown stage {from_stage!r}; known: {', '.join(STAGES)}")
        raise typer.Exit(code=2)
    db.update_job(job_id, render_version=int(job.get("render_version") or 1) + 1)
    worker = Worker(db, cfg)
    if not worker._acquire_lock():  # noqa: SLF001
        db.update_job(job_id, state="queued", pending_from=from_stage)
        echo("a worker is already running; queued the rerender for it")
        return
    try:
        run_job(db, cfg, job_id, RunOptions(from_stage=from_stage))
    finally:
        worker.stop()
    echo(f"rerendered {job_id} from {from_stage}")


@app.command()
def preview(
    job_id: str,
    config: str | None = CONFIG_OPTION,
    minutes: float = typer.Option(3.0, "--minutes", "-m"),
    play: bool = typer.Option(False, "--play"),
) -> None:
    """Re-prepare and synthesize the first few minutes. The feed is untouched."""
    cfg, db = _setup(config)
    out_dir = cfg.jobs_dir / job_id / "preview"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_job(
        db,
        cfg,
        job_id,
        RunOptions(
            from_stage="preparing",
            preview_minutes=minutes,
            publish=False,
            out_dir=out_dir,
            verify=False,
        ),
    )
    episode = out_dir / "episode.mp3"
    echo(f"preview: {episode}")
    if play:
        _play(episode)


# -- audition ---------------------------------------------------------------


@app.command()
def say(
    text: list[str] = typer.Argument(None, help="Text to speak."),
    config: str | None = CONFIG_OPTION,
    compare: list[str] = typer.Option(None, "--compare", help="Speak each of these in turn."),
    backend: str | None = typer.Option(None, "--backend", "-b"),
    voice: str | None = typer.Option(None, "--voice", "-v"),
    play: bool = typer.Option(False, "--play"),
) -> None:
    """Speak a string. Pick a respelling by ear instead of re-rendering an episode."""
    cfg, _ = _setup(config)
    from readcast.synth import get_backend

    phrases = list(compare) if compare else []
    joined = " ".join(text or []).strip()
    if joined:
        phrases.insert(0, joined)
    if not phrases:
        echo("nothing to say")
        raise typer.Exit(code=2)

    name = backend or cfg["tts"]["backend"]
    engine = get_backend(name, cfg.backend_settings(name))
    out_dir = Path(tempfile.mkdtemp(prefix="readcast-say-"))
    for i, phrase in enumerate(phrases):
        t0 = time.perf_counter()
        audio = engine.synth(
            phrase,
            voice=voice or cfg["tts"].get("voice", "default"),
            instruction=(cfg["tts"].get("instructions") or {}).get("body"),
        )
        path = out_dir / f"{i:02d}.wav"
        path.write_bytes(audio)
        echo(f"  {i + 1}. {phrase!r}  ({time.perf_counter() - t0:.1f}s)  {path}")
        if play:
            _play(path)


# -- rules ------------------------------------------------------------------


@rules_app.command("test")
def rules_test(config: str | None = CONFIG_OPTION) -> None:
    """Run rules/tests.yml. Exits non-zero on any failure."""
    from readcast.prepare.tests_runner import format_report, run_cases

    cfg = load_config(config)
    results = run_cases(cfg.rules_dir)
    report, failed = format_report(results)
    echo(report)
    for result in results:
        if not result.passed:
            echo("")
            echo(f"--- {result.name}")
            echo(result.diff())
    raise typer.Exit(code=1 if failed else 0)


# -- lexicon ----------------------------------------------------------------


def _parse_since(value: str) -> datetime:
    m = re.fullmatch(r"(\d+)\s*([dhwm])", value.strip().lower())
    if not m:
        raise typer.BadParameter("use a form like 30d, 12h, 4w")
    amount, unit = int(m.group(1)), m.group(2)
    delta = {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
        "m": timedelta(days=30 * amount),
    }[unit]
    return datetime.now(timezone.utc) - delta


@lexicon_app.command("suggest")
def lexicon_suggest(
    config: str | None = CONFIG_OPTION,
    since: str = typer.Option("30d", "--since"),
    min_count: int = typer.Option(2, "--min-count"),
    limit: int = typer.Option(40, "--limit"),
) -> None:
    """Aggregate unknowns.jsonl across jobs into a work queue.

    Never writes to lexicon.yml. The operator decides and commits.
    """
    cfg, db = _setup(config)
    cutoff = _parse_since(since)
    totals: dict[str, dict[str, Any]] = {}
    for job in db.list_jobs(limit=10000):
        submitted = str(job.get("submitted_at") or "")
        try:
            when = datetime.fromisoformat(submitted)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            continue
        path = cfg.jobs_dir / str(job["id"]) / "unknowns.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            entry = totals.setdefault(
                row["term"],
                {"count": 0, "jobs": 0, "suggested": row.get("suggested", "respell"),
                 "example": row.get("example", "")},
            )
            entry["count"] += int(row.get("count", 1))
            entry["jobs"] += 1

    rows = [
        (term, data)
        for term, data in totals.items()
        if data["count"] >= min_count
    ]
    rows.sort(key=lambda kv: (-kv[1]["count"], kv[0]))
    if not rows:
        echo(f"nothing above --min-count {min_count} since {since}")
        return
    echo(f"{'term':16} {'count':>5} {'jobs':>5}  {'suggested':10} example")
    for term, data in rows[:limit]:
        example = data["example"]
        if len(example) > 60:
            example = example[:57] + "..."
        echo(
            f"{term[:16]:16} {data['count']:5} {data['jobs']:5}  "
            f"{data['suggested']:10} \"{example}\""
        )
    echo("")
    echo("Add the ones worth fixing to rules/lexicon.yml, then `readcast rules test`.")


# -- compare ----------------------------------------------------------------


@app.command()
def compare(
    url: str,
    config: str | None = CONFIG_OPTION,
    backends: str = typer.Option(..., "--backends", help="Comma separated, e.g. mlx,elevenlabs"),
    minutes: float = typer.Option(4.0, "--minutes", "-m"),
) -> None:
    """Prepare once, synthesize the opening with each backend, print the numbers."""
    from readcast.assemble import assemble, build_intro, ensure_cover
    from readcast.chunk import plan_chunks
    from readcast.synth import get_backend
    from readcast.synth.runner import synthesize_chunks

    cfg, db = _setup(config)
    names = [n.strip() for n in backends.split(",") if n.strip()]
    job_id = db.create_job(id=new_job_id(), url=url, backend=names[0])
    directory = job_dir(cfg, job_id)
    write_job_json(directory, {"id": job_id, "url": url, "compare": names})

    run_job(db, cfg, job_id, RunOptions(from_stage="fetching", publish=False,
                                        out_dir=directory, verify=False,
                                        preview_minutes=0.001))
    job = db.get_job(job_id) or {}
    spoken = (directory / "spoken.txt").read_text()
    full_chars = len(spoken)

    meta = {k: job.get(k) for k in ("title", "author", "publication", "published_at")}
    meta["url"] = url
    intro = build_intro(str(cfg["feed"]["intro_template"]), meta)
    all_chunks = plan_chunks(
        spoken,
        target_chars=int(cfg["chunk"]["target_chars"]),
        max_chars=int(cfg["chunk"]["max_chars"]),
        intro=intro or None,
    )
    budget = minutes * 60 * 15
    chunks, used = [], 0
    for chunk in all_chunks:
        if used > budget:
            break
        chunks.append(chunk)
        used += len(chunk.text)

    out_root = cfg.root / "compare" / job_id
    out_root.mkdir(parents=True, exist_ok=True)
    cover = ensure_cover(cfg.feed_dir / "cover.jpg", str(cfg["feed"]["title"]))

    results = []
    for name in names:
        echo(f"synthesizing {len(chunks)} chunks on {name} ...")
        settings = cfg.backend_settings(name)
        try:
            backend = get_backend(name, settings)
            synth = synthesize_chunks(
                chunks,
                backend,
                out_root / name,
                voice=str(settings.get("voice") or "default"),
                instructions=dict(cfg["tts"].get("instructions") or {}),
                verify=dict(cfg["verify"]) | {"enabled": False},
            )
            built = assemble(
                synth.chunks,
                out_root / f"{name}.mp3",
                meta=meta,
                audio_cfg=dict(cfg["audio"]),
                cover=cover,
            )
            cost_per_1k = float(settings.get("cost_per_1k_chars",
                                             getattr(backend, "cost_per_1k_chars", 0.0)))
            results.append(
                {
                    "backend": name,
                    "rtf": synth.rtf,
                    "wall_s": synth.synth_seconds,
                    "flagged": synth.flagged_chunks,
                    "audio_s": built.duration_s,
                    "cost": cost_per_1k * full_chars / 1000.0,
                    "path": built.path,
                }
            )
        except Exception as exc:  # noqa: BLE001 - a backend that fails still gets a row
            echo(f"  {name} failed: {exc}")
            results.append({"backend": name, "error": str(exc)})

    echo("")
    echo(f"{'backend':12} {'rtf':>6} {'wall s':>8} {'audio s':>8} {'flagged':>8} {'est. $/episode':>15}")
    for row in results:
        if "error" in row:
            echo(f"{row['backend']:12} {'—':>6} {'—':>8} {'—':>8} {'—':>8} {'—':>15}  {row['error'][:40]}")
            continue
        echo(
            f"{row['backend']:12} {row['rtf']:6.2f} {row['wall_s']:8.1f} "
            f"{row['audio_s']:8.1f} {row['flagged']:8} {row['cost']:15.2f}"
        )
    echo("")
    echo(f"files in {out_root}")
    echo(f"cost is for the whole article ({full_chars} characters), not the sample.")


# -- feed -------------------------------------------------------------------


@feed_app.command("rotate")
def feed_rotate(config: str | None = CONFIG_OPTION) -> None:
    """Issue a new feed token. The old URL stops working."""
    cfg, db = _setup(config)
    token = db.feed_token(rotate=True)
    feed_mod.write_feed(db, cfg)
    echo("new feed url (resubscribe in the podcast app):")
    echo(f"  {feed_mod.feed_url(cfg.base_url, token)}")


@feed_app.command("rebuild")
def feed_rebuild(config: str | None = CONFIG_OPTION) -> None:
    """Regenerate feed.xml from the database."""
    cfg, db = _setup(config)
    path = feed_mod.write_feed(db, cfg)
    echo(f"wrote {path}")


@feed_app.command("url")
def feed_show_url(config: str | None = CONFIG_OPTION) -> None:
    """Print the current feed URL."""
    cfg, db = _setup(config)
    echo(feed_mod.feed_url(cfg.base_url, db.feed_token()))


# -- bookmarklet ------------------------------------------------------------

BOOKMARKLET = """javascript:(async()=>{
  const B='__BASE__',T='__TOKEN__',
        H=document.documentElement.outerHTML.slice(0,4e6),
        U=location.href,I=document.title;
  const toast=(m,ok)=>{const t=document.createElement('div');
    t.textContent='readcast: '+m;
    t.style.cssText='position:fixed;top:12px;right:12px;z-index:2147483647;padding:8px 12px;background:'+(ok?'#111':'#7a1f2b')+';color:#fff;font:13px system-ui;border-radius:6px';
    document.body.appendChild(t);setTimeout(()=>t.remove(),2500);};
  try{
    const r=await fetch(B+'/jobs',{method:'POST',mode:'cors',
      headers:{'content-type':'application/json','authorization':'Bearer '+T},
      body:JSON.stringify({url:U,title:I,client_html:H})});
    toast(r.ok?'queued':'failed ('+r.status+')',r.ok);
  }catch(e){
    // The page's CSP blocks fetch. A form POST is governed by form-action,
    // which has no default-src fallback, so it still gets through.
    const f=document.createElement('form');
    f.method='POST';f.action=B+'/jobs/form';f.target='_blank';
    f.style.display='none';
    for(const [k,v] of Object.entries({url:U,title:I,client_html:H,token:T})){
      const i=document.createElement('input');i.type='hidden';i.name=k;i.value=v;f.appendChild(i);}
    document.body.appendChild(f);f.submit();f.remove();
    toast('queued in a new tab',true);
  }
})()"""


@app.command()
def bookmarklet(
    config: str | None = CONFIG_OPTION,
    minify: bool = typer.Option(True, help="Collapse to one line for the bookmark field."),
    base: str | None = typer.Option(
        None,
        "--base",
        help=(
            "Where the browser posts, when that differs from base_url. Use "
            "http://127.0.0.1:PORT so the browser treats it as a secure origin "
            "while the feed keeps a LAN or tailnet address."
        ),
    ),
) -> None:
    """Print the bookmarklet. Drag it to the bookmarks bar."""
    cfg, _ = _setup(config)
    target = (base or cfg.base_url).rstrip("/")
    code = BOOKMARKLET.replace("__BASE__", target).replace("__TOKEN__", cfg.api_token)
    if minify:
        code = re.sub(r"\n\s*", "", code)
    echo(code)
    echo("")
    if target.startswith("http://") and "localhost" not in target \
            and "127.0.0.1" not in target:
        echo("warning: the base_url is plain HTTP. A browser blocks that from an HTTPS")
        echo("page. Put the service behind Tailscale Serve and set base_url to the")
        echo("https://<host>.ts.net URL.")
    if cfg.api_token == "CHANGE_ME":
        echo("warning: set api_token in config.yml before using this.")


@app.command()
def ui(
    config: str | None = CONFIG_OPTION,
    base: str | None = typer.Option(None, "--base", help="Override the host in the URL."),
    no_token: bool = typer.Option(False, "--no-token", help="Print the bare URL."),
) -> None:
    """Print the URL of the web dashboard, with the token filled in."""
    cfg, _ = _setup(config)
    target = (base or cfg.base_url).rstrip("/")
    if no_token:
        echo(f"{target}/ui")
        return
    echo(f"{target}/ui#token={cfg.api_token}")
    echo("")
    echo("The token is stored in that browser and stripped from the URL.")


@app.command()
def prep(
    target: str,
    config: str | None = CONFIG_OPTION,
    from_stage: str | None = typer.Option(
        None, "--from",
        help="fetching, extracting or preparing. Defaults to whatever is on disk.",
    ),
) -> None:
    """Fetch, extract and prepare — text only, no audio.

    Takes a URL or an existing job id. The job lands in `ready` with
    spoken.txt on disk, so the text can be reviewed and the rules tuned before
    anything expensive happens.
    """
    cfg, db = _setup(config)
    if not target.startswith("http"):
        existing = db.get_job(target)
        if existing and existing.get("text_edited"):
            db.update_job(target, text_edited=0)   # an explicit re-prepare
    if target.startswith("http://") or target.startswith("https://"):
        job_id = db.create_job(id=new_job_id(), url=target)
        write_job_json(job_dir(cfg, job_id), {"id": job_id, "url": target})
        start = "fetching"
    else:
        job_id = target
        if db.get_job(job_id) is None:
            echo(f"no such job: {job_id}")
            raise typer.Exit(code=1)
        # Re-prepare from the rules when the extraction is already on disk;
        # otherwise this job has not been read yet, so start from the top.
        directory = cfg.jobs_dir / job_id
        if from_stage:
            if from_stage not in ("fetching", "extracting", "preparing"):
                echo("--from takes fetching, extracting or preparing")
                raise typer.Exit(code=2)
            start = from_stage
            worker = PrepWorker(db, cfg)
            if not worker._acquire_lock():  # noqa: SLF001
                db.update_job(job_id, state="queued", pending_from=start)
                echo(f"{job_id} queued from {start}")
                return
            try:
                run_job(db, cfg, job_id,
                        RunOptions(from_stage=start, until_stage="preparing"))
            finally:
                worker.stop()
            job = db.get_job(job_id) or {}
            echo(f"{job_id} {job.get('state')} — {job.get('title') or job.get('url')}")
            return
        raw_html = directory / "raw.html"
        usable_html = raw_html.is_file() and not _is_pdf_shell(raw_html, job_url(db, job_id))
        if (directory / "extracted.md").is_file():
            start = "preparing"
        elif (directory / "raw.pdf").is_file() or usable_html:
            start = "extracting"
        else:
            # Nothing usable was fetched: a PDF viewer shell is not a page.
            start = "fetching"

    worker = PrepWorker(db, cfg)
    if not worker._acquire_lock():  # noqa: SLF001
        db.update_job(job_id, state="queued", pending_from=start)
        echo(f"{job_id} queued; the running prep worker will pick it up")
        return
    try:
        run_job(db, cfg, job_id, RunOptions(from_stage=start, until_stage="preparing"))
    finally:
        worker.stop()
    job = db.get_job(job_id) or {}
    if job.get("state") == "ready":
        echo(f"{job_id} ready — {job.get('word_count') or 0} words")
        echo(f"  text:  readcast show {job_id}")
        echo(f"  file:  {cfg.jobs_dir / job_id / 'spoken.txt'}")
        echo(f"  audio: readcast release {job_id}" if job.get("hold")
             else "  the audio lane will pick it up")
    else:
        echo(f"{job.get('state')}: {job.get('error') or ''}")
        raise typer.Exit(code=1)


@app.command("hold-new")
def hold_new(
    setting: str | None = typer.Argument(None, help="on, off, or nothing to show."),
    config: str | None = CONFIG_OPTION,
) -> None:
    """Whether new articles wait for a look before they are rendered."""
    from readcast.config import hold_new_jobs, set_hold_new_jobs

    cfg, db = _setup(config)
    if setting is None:
        state = hold_new_jobs(cfg, db)
        echo(f"new articles: {'hold for review' if state else 'render automatically'}")
        return
    if setting.lower() not in ("on", "off", "true", "false", "yes", "no"):
        echo("say 'on' to hold new articles, or 'off' to render them automatically")
        raise typer.Exit(code=2)
    value = setting.lower() in ("on", "true", "yes")
    set_hold_new_jobs(db, value)
    echo(
        "new articles will wait for you; release them with `readcast release <id>`"
        if value else
        "new articles will render as soon as their text is ready"
    )


@app.command()
def hold(job_id: str, config: str | None = CONFIG_OPTION) -> None:
    """Keep a prepared job out of the audio lane."""
    _, db = _setup(config)
    if db.get_job(job_id) is None:
        echo(f"no such job: {job_id}")
        raise typer.Exit(code=1)
    db.update_job(job_id, hold=1)
    echo(f"{job_id} held — it will not synthesize until released")


@app.command()
def release(job_id: str, config: str | None = CONFIG_OPTION) -> None:
    """Send a held job to the audio lane."""
    _, db = _setup(config)
    job = db.get_job(job_id)
    if job is None:
        echo(f"no such job: {job_id}")
        raise typer.Exit(code=1)
    fields: dict[str, object] = {"hold": 0}
    if job["state"] not in ("ready", "queued"):
        fields["state"] = "ready"   # re-offer a finished or failed job to the lane
    db.update_job(job_id, **fields)
    echo(f"{job_id} released to the audio lane")


@app.command()
def cancel(job_id: str, config: str | None = CONFIG_OPTION) -> None:
    """Stop a render in progress, or drop a waiting job from the queue."""
    _, db = _setup(config)
    job = db.get_job(job_id)
    if job is None:
        echo(f"no such job: {job_id}")
        raise typer.Exit(code=1)
    state = str(job["state"])
    if state in ("synthesizing", "assembling"):
        db.update_job(job_id, cancel_requested=1)
        echo(f"asked the worker to stop {job_id}; it stops after the current chunk")
        echo("Audio already made is kept: `readcast release` resumes from there.")
        return
    if state in ("queued", "ready", "fetching", "extracting", "preparing"):
        db.update_job(job_id, state="cancelled", hold=0, error="taken out of the queue")
        echo(f"{job_id} taken out of the queue")
        return
    echo(f"a {state} job is not running or queued")
    raise typer.Exit(code=1)


@app.command()
def remove(
    job_id: str,
    config: str | None = CONFIG_OPTION,
    files: bool = typer.Option(False, "--files", help="Delete its artifacts too."),
) -> None:
    """Remove a job. Its episode leaves the feed."""
    import shutil

    cfg, db = _setup(config)
    job = db.get_job(job_id)
    if job is None:
        echo(f"no such job: {job_id}")
        raise typer.Exit(code=1)
    if job["state"] in ("synthesizing", "assembling"):
        echo("that job is rendering; `readcast cancel` it first")
        raise typer.Exit(code=1)
    if files:
        shutil.rmtree(cfg.jobs_dir / job_id, ignore_errors=True)
    db.delete_job(job_id)
    feed_mod.write_feed(db, cfg)
    echo(f"removed {job_id}{' and its files' if files else ''}")


@app.command()
def show(
    job_id: str,
    config: str | None = CONFIG_OPTION,
    chunks: bool = typer.Option(False, "--chunks", help="Show the per-chunk breakdown."),
    raw: bool = typer.Option(False, "--raw", help="Print spoken.txt with no framing."),
) -> None:
    """Show the text a job hands to the speech engine."""
    from rich.console import Console

    from readcast.dashboard import job_artifacts, job_detail

    cfg, db = _setup(config)
    job = db.get_job(job_id)
    if job is None:
        echo(f"no such job: {job_id}")
        raise typer.Exit(code=1)
    if raw:
        path = job_artifacts(cfg, job)["spoken"]
        if not path.is_file():
            echo(f"{job_id} has no spoken.txt yet (state: {job['state']})")
            raise typer.Exit(code=1)
        typer.echo(path.read_text(), nl=False)
        return
    Console().print(job_detail(cfg, db, job, chunks=chunks))


@voices_app.command("init")
def voices_init(
    config: str | None = CONFIG_OPTION,
    candidates: int = typer.Option(6, "--candidates", "-n", help="Voices to sample."),
    backend: str | None = typer.Option(None, "--backend", "-b"),
    play: bool = typer.Option(False, "--play", help="Play each candidate."),
    force: bool = typer.Option(False, "--force", help="Replace existing voices."),
) -> None:
    """Record one reference clip per role, so the voice stops wandering.

    The model picks a new speaker for every request unless it is conditioned on
    a clip. This samples a handful, keeps the ones that sound most unlike each
    other, and assigns them to the narrator, quotations and asides.
    """
    from readcast.synth import get_backend
    from readcast.voices import (
        REFERENCE_TEXT, choose_distinct, estimate_pitch, load_voices, render_reference,
        voice_dir,
    )

    cfg, _ = _setup(config)
    voices = load_voices(cfg)
    roles: list[str] = []
    for voice in voices.values():
        if voice.role not in roles:
            roles.append(voice.role)
    existing = [v.role for v in voices.values() if v.available]
    if existing and not force:
        echo(f"already recorded: {', '.join(sorted(set(existing)))}")
        echo("re-record with --force, or change one with `readcast voices set`")
        raise typer.Exit(code=0)

    name = backend or cfg["tts"]["backend"]
    engine = get_backend(name, cfg.backend_settings(name))
    text = str(cfg["tts"].get("reference_text") or REFERENCE_TEXT)
    pool = voice_dir(cfg) / "candidates"
    pool.mkdir(parents=True, exist_ok=True)

    echo(f"sampling {candidates} voices from {name} (each is one short synthesis) ...")
    measured: list[tuple[Path, float | None]] = []
    for i in range(candidates):
        path = pool / f"{i:02d}.wav"
        try:
            render_reference(engine, text, path)
        except Exception as exc:  # noqa: BLE001 - report and stop, do not traceback
            echo(f"  candidate {i}: failed — {exc}")
            raise typer.Exit(code=1) from exc
        hz = estimate_pitch(path)
        measured.append((path, hz))
        echo(f"  candidate {i}: {path.name}  {f'{hz:.0f} Hz' if hz else 'unmeasured'}")
        if play:
            _play(path)

    chosen = choose_distinct(measured, len(roles))
    for role, source in zip(roles, chosen):
        target = voice_dir(cfg) / f"{role}.wav"
        target.write_bytes(source.read_bytes())
        hz = estimate_pitch(target)
        echo(f"{role:6} <- {source.name}  {f'{hz:.0f} Hz' if hz else ''}")
    echo("")
    echo("Listen with `readcast voices play <role>`; swap one with")
    echo("`readcast voices set <role> <candidate>`. Then re-render an episode.")


@voices_app.command("sample")
def voices_sample(
    config: str | None = CONFIG_OPTION,
    count: int = typer.Option(4, "--count", "-n", help="New voices to add."),
    backend: str | None = typer.Option(None, "--backend", "-b"),
    play: bool = typer.Option(False, "--play"),
) -> None:
    """Add voices to the pool. Each article picks one and keeps it."""
    from readcast.synth import get_backend
    from readcast.voices import (
        REFERENCE_TEXT, estimate_pitch, pool_dir, pool_voices, render_reference,
    )

    cfg, db = _setup(config)
    pool = pool_dir(cfg)
    pool.mkdir(parents=True, exist_ok=True)
    existing = pool_voices(cfg)
    start = max((int(p.stem) for p in existing if p.stem.isdigit()), default=-1) + 1

    name = backend or cfg["tts"]["backend"]
    engine = get_backend(name, cfg.backend_settings(name))
    text = str(cfg["tts"].get("reference_text") or REFERENCE_TEXT)

    # Take the audio lane's lock. Two things generating at once makes the model
    # babble, which would quietly corrupt whatever episode is rendering.
    lane = Worker(db, cfg)
    if not lane._acquire_lock():  # noqa: SLF001
        echo("the audio lane is busy rendering an episode.")
        echo("Sampling now would corrupt it, so this waits for the lane to free up.")
        echo("Hold the queue (`readcast hold <id>`) or try again later.")
        raise typer.Exit(code=1)

    echo(f"sampling {count} voices into {pool} ({len(existing)} already there) ...")
    try:
        for i in range(start, start + count):
            path = pool / f"{i:02d}.wav"
            try:
                render_reference(engine, text, path)
            except Exception as exc:  # noqa: BLE001
                echo(f"  {path.name}: failed — {exc}")
                raise typer.Exit(code=1) from exc
            hz = pitch_of(path)
            echo(f"  {path.name}  {f'{hz:.0f} Hz' if hz else ''}")
            if play:
                _play(path)
    finally:
        lane.stop()
    echo("")
    echo("Press the re-roll button on a queued episode to hear a random one.")


@voices_app.command("audition")
def voices_audition(
    config: str | None = CONFIG_OPTION,
    backend: str | None = typer.Option(None, "--backend", "-b"),
    voices: str | None = typer.Option(None, "--voices", help="Comma separated subset."),
    prefixes: str = typer.Option(
        "af_,am_,bf_,bm_", "--prefixes",
        help="Which voice packs to include, by name prefix (language and gender).",
    ),
    play: bool = typer.Option(False, "--play"),
) -> None:
    """Render every named voice saying its own name, as one file to listen through.

    Only worth doing on a fast backend: it is one synthesis per voice.
    """
    from readcast.audio import concat_wavs, to_wav_mono, write_silence
    from readcast.synth import get_backend
    from readcast.voices import REFERENCE_TEXT

    cfg, _ = _setup(config)
    name = backend or cfg["tts"]["backend"]
    settings = cfg.backend_settings(name)
    engine = get_backend(name, settings)
    if getattr(engine, "supports_reference", False):
        echo(f"{name} clones a voice from a clip rather than naming one.")
        echo("Use `readcast voices sample` and `readcast voices play all` instead.")
        raise typer.Exit(code=1)

    if voices:
        names = [v.strip() for v in voices.split(",") if v.strip()]
    else:
        import httpx

        url = str(settings.get("url") or "").rstrip("/")
        try:
            listed = httpx.get(f"{url}/audio/voices",
                               params={"model": settings.get("model")}, timeout=30).json()
            names = [v["id"] for v in listed.get("data", [])]
        except Exception as exc:  # noqa: BLE001
            echo(f"could not list voices from {url}: {exc}")
            raise typer.Exit(code=1) from exc
        wanted = tuple(p.strip() for p in prefixes.split(",") if p.strip())
        if wanted:
            names = [n for n in names if n.startswith(wanted)]

    out_dir = cfg.data_dir / "voices" / "audition"
    out_dir.mkdir(parents=True, exist_ok=True)
    sentence = str(cfg["tts"].get("reference_text") or REFERENCE_TEXT).split(".")[0] + "."
    parts, gap = [], write_silence(out_dir / "gap.wav", 450)

    echo(f"auditioning {len(names)} voices on {name} ...")
    for voice in names:
        path = out_dir / f"{voice}.wav"
        try:
            path.write_bytes(to_wav_mono(
                engine.synth(f"{voice.replace('_', ' ')}. {sentence}", voice=voice)))
        except Exception as exc:  # noqa: BLE001
            echo(f"  {voice}: failed — {str(exc)[:60]}")
            continue
        echo(f"  {voice}")
        parts += [path, gap]
        if play:
            _play(path)

    if not parts:
        echo("nothing rendered")
        raise typer.Exit(code=1)
    track = out_dir / "audition.wav"
    concat_wavs(parts, track)
    echo("")
    echo(f"one track with all of them: {track}")
    echo("Put the ones you like in tts.backends.<backend>.voices in config.yml.")


@voices_app.command("show")
def voices_show(config: str | None = CONFIG_OPTION) -> None:
    """The pool, and who read what."""
    from readcast.voices import load_voices, pitch_of, pool_dir, pool_voices

    cfg, db = _setup(config)
    mode = str(cfg["tts"].get("voice_mode", "per_episode"))
    echo(f"mode: {mode}")

    if mode == "per_episode":
        pool = pool_voices(cfg)
        if not pool:
            echo(f"the pool at {pool_dir(cfg)} is empty — run `readcast voices sample`")
            raise typer.Exit(code=0)
        used = {}
        for job in db.list_jobs(limit=200):
            if job.get("voice_main"):
                used.setdefault(str(job["voice_main"]), []).append(job)
        echo("")
        echo(f"{'voice':9} {'pitch':>7} {'episodes':>9}  last read")
        for path in pool:
            hz = pitch_of(path)
            jobs = used.get(path.name, [])
            last = str(jobs[0].get("title") or jobs[0]["id"])[:44] if jobs else "—"
            echo(f"{path.name:9} {(f'{hz:.0f} Hz' if hz else '—'):>7} {len(jobs):>9}  {last}")
        return

    voices = load_voices(cfg)
    echo("")
    echo(f"{'kind':9} {'role':7} {'pitch':>7}  file")
    for kind, voice in voices.items():
        hz = pitch_of(voice.audio) if voice.available else None
        echo(
            f"{kind:9} {voice.role:7} {(f'{hz:.0f} Hz' if hz else '—'):>7}  "
            f"{voice.audio if voice.available else 'not recorded'}"
        )


@voices_app.command("assign")
def voices_assign(
    job_id: str,
    voice: str,
    config: str | None = CONFIG_OPTION,
) -> None:
    """Pin a narrator to one job, by pool file name or number."""
    from readcast.voices import choose_cast, pool_dir, pool_voices

    cfg, db = _setup(config)
    if db.get_job(job_id) is None:
        echo(f"no such job: {job_id}")
        raise typer.Exit(code=1)
    name = voice if voice.endswith(".wav") else f"{int(voice):02d}.wav"
    path = pool_dir(cfg) / name
    if not path.is_file():
        echo(f"no voice {name} in {pool_dir(cfg)}")
        raise typer.Exit(code=1)
    pool = pool_voices(cfg)
    cast = choose_cast([path] + [p for p in pool if p != path], recent=[])
    db.update_job(
        job_id, voice_main=path.name,
        voice_quote=cast["quote"].name, voice_aside=cast["aside"].name,
    )
    echo(f"{job_id} will be read by {name}")
    echo("Rerender it from synthesizing to hear the change.")


@voices_app.command("play")
def voices_play(
    which: str = typer.Argument("all", help="A pool voice (number or file), or a role."),
    config: str | None = CONFIG_OPTION,
) -> None:
    """Play a pool voice, a role's voice, or every voice in turn."""
    from readcast.voices import load_voices, pool_dir, pool_voices

    cfg, _ = _setup(config)
    if which == "all":
        paths = pool_voices(cfg)
    elif which.rstrip(".wav").isdigit() or which.endswith(".wav"):
        name = which if which.endswith(".wav") else f"{int(which):02d}.wav"
        paths = [pool_dir(cfg) / name]
    else:
        match = next((v for v in load_voices(cfg).values() if v.role == which), None)
        paths = [match.audio] if match and match.available else []
    paths = [Path(p) for p in paths if p and Path(p).is_file()]
    if not paths:
        echo(f"nothing to play for {which!r}")
        raise typer.Exit(code=1)
    for path in paths:
        echo(str(path.name))
        _play(path)


@voices_app.command("set")
def voices_set(
    role: str,
    candidate: int,
    config: str | None = CONFIG_OPTION,
) -> None:
    """Assign a sampled candidate to a role."""
    from readcast.voices import estimate_pitch, voice_dir

    cfg, _ = _setup(config)
    source = voice_dir(cfg) / "candidates" / f"{candidate:02d}.wav"
    if not source.is_file():
        echo(f"no candidate {candidate}; run `readcast voices init` first")
        raise typer.Exit(code=1)
    target = voice_dir(cfg) / f"{role}.wav"
    target.write_bytes(source.read_bytes())
    hz = estimate_pitch(target)
    echo(f"{role} <- {source.name}  {f'{hz:.0f} Hz' if hz else ''}")
    echo("Existing episodes keep their old voice until you rerender them.")


@app.command("extension-link")
def extension_link(
    extension_id: str = typer.Argument(
        ..., help="From chrome://extensions with Developer mode on."
    ),
    config: str | None = CONFIG_OPTION,
    base: str | None = typer.Option(None, "--base", help="Host the browser posts to."),
) -> None:
    """Print a one-paste setup link for a Chrome profile.

    Extension settings are stored per profile, so every Chrome session needs the
    token once. Paste this into that profile's address bar instead of typing it.
    """
    cfg, _ = _setup(config)
    target = (base or cfg.base_url).rstrip("/")
    ident = extension_id.strip().strip("/").split("/")[-1]
    echo(
        f"chrome-extension://{ident}/options.html"
        f"#host={target}&token={cfg.api_token}"
    )
    echo("")
    echo("Paste that into the address bar of each Chrome profile you use.")
    echo("It saves the settings, clears itself from the URL, and tests the")
    echo("connection. The fragment after # never leaves the browser.")


@app.command()
def init(config: str | None = CONFIG_OPTION) -> None:
    """Create the data directories, the cover, and the feed."""
    from readcast.assemble import ensure_cover

    cfg, db = _setup(config)
    ensure_cover(cfg.feed_dir / "cover.jpg", str(cfg["feed"]["title"]))
    feed_mod.write_feed(db, cfg)
    echo(f"data dir: {cfg.data_dir}")
    echo(f"feed:     {feed_mod.feed_url(cfg.base_url, db.feed_token())}")
    echo(f"status:   {cfg.base_url}/status")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
