"""The HTTP surface.

One POST to start a job, a read-only status page, and the feed. The feed and
the audio are served under a token path because a podcast client cannot send an
Authorization header.
"""

from __future__ import annotations

import html
import logging
import mimetypes
import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator

from fastapi import (
    Body, Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from readcast import feed as feed_mod
from readcast.config import Config, hold_new_jobs, load_config, set_hold_new_jobs
from readcast.db import STAGES, Database, new_job_id, utcnow
from readcast.worker import PrepWorker, Worker, job_dir, write_job_json

log = logging.getLogger("readcast.api")

NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}


class QueueOrder(BaseModel):
    order: list[str]


class TextEdit(BaseModel):
    text: str


class JobRequest(BaseModel):
    url: str
    title: str | None = None
    author: str | None = None
    publication: str | None = None
    client_html: str | None = Field(default=None, repr=False)
    backend: str | None = None
    voice: str | None = None


def create_app(
    cfg: Config | None = None, *, start_worker: bool = True, db: Database | None = None
) -> FastAPI:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    database = db or Database(cfg.db_path)

    app = FastAPI(title="readcast", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.db = database
    app.state.worker = None

    # The bookmarklet posts from whatever page the operator is reading.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["authorization", "content-type"],
        max_age=86400,
    )

    @app.middleware("http")
    async def allow_private_network(request: Request, call_next):
        """Chrome sends this preflight for an https page reaching localhost.

        Without the matching response header, Private Network Access blocks the
        bookmarklet before the POST is ever made.
        """
        response = await call_next(request)
        if request.headers.get("access-control-request-private-network"):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    app.state.prep_worker = None
    if start_worker:
        # Two lanes: text preparation is cheap and runs immediately, so the
        # spoken text is on disk seconds after submitting. Audio stays serial.
        prep = PrepWorker(database, cfg, poll_seconds=0.5)
        prep.start(wait_for_lock=True)
        app.state.prep_worker = prep
        worker = Worker(database, cfg)
        worker.start(wait_for_lock=True)
        app.state.worker = worker

        @app.on_event("shutdown")
        def _stop_worker() -> None:  # pragma: no cover - lifecycle
            for w in (app.state.worker, app.state.prep_worker):
                if w:
                    w.stop()

    def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = cfg.api_token
        supplied = ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not expected or expected == "CHANGE_ME":
            raise HTTPException(
                status_code=503,
                detail="api_token is not set in config.yml; refusing to accept jobs",
            )
        if supplied != expected:
            raise HTTPException(status_code=401, detail="bad token")

    def check_feed_token(token: str) -> None:
        if token != database.feed_token():
            # A rotated token must read as gone, not as forbidden.
            raise HTTPException(status_code=404, detail="not found")

    # -- jobs ---------------------------------------------------------------

    def _queue(payload: JobRequest) -> str:
        client_html = payload.client_html or ""
        job_id = new_job_id()
        directory = job_dir(cfg, job_id)
        if client_html:
            (directory / "raw.html").write_text(client_html)
        write_job_json(
            directory,
            {
                "id": job_id,
                "url": payload.url,
                "title": payload.title,
                "backend": payload.backend or cfg["tts"]["backend"],
                "voice": payload.voice or cfg["tts"].get("voice"),
                "client_html": bool(client_html),
                "submitted_at": utcnow(),
            },
        )
        database.create_job(
            id=job_id,
            url=payload.url,
            title=payload.title,
            author=payload.author,
            publication=payload.publication,
            backend=payload.backend,   # None means "whatever is configured"
            voice=payload.voice,
            client_html=int(bool(client_html)),
            # A title the submitter chose outranks anything extraction finds.
            title_locked=int(bool(payload.title)),
        )
        log.info("queued %s for %s (client_html=%s)", job_id, payload.url, bool(client_html))
        return job_id

    @app.post("/jobs", status_code=202, dependencies=[Depends(require_token)])
    def submit(payload: JobRequest = Body(...)) -> dict[str, str]:
        limit = int(cfg["fetch"]["max_client_html_bytes"])
        if len((payload.client_html or "").encode("utf-8", errors="ignore")) > limit:
            raise HTTPException(status_code=413, detail="client_html is too large")
        return {"id": _queue(payload)}

    @app.post("/jobs/form", response_class=HTMLResponse)
    def submit_form(
        url: str = Form(...),
        token: str = Form(...),
        title: str | None = Form(default=None),
        client_html: str | None = Form(default=None),
    ) -> HTMLResponse:
        """Form-encoded submit, for pages whose CSP blocks fetch.

        A bookmarklet's `fetch` runs in the page's origin and obeys the page's
        `connect-src`; Wikipedia and many news sites forbid it outright. CSP's
        `form-action` has no `default-src` fallback, so a form POST still gets
        through. The token travels as a field because a form cannot set headers.
        """
        if not cfg.api_token or cfg.api_token == "CHANGE_ME":
            return HTMLResponse(_receipt("readcast is not configured", "Set api_token in config.yml."), status_code=503)
        if token != cfg.api_token:
            return HTMLResponse(_receipt("Rejected", "Bad token."), status_code=401)
        limit = int(cfg["fetch"]["max_client_html_bytes"])
        if len((client_html or "").encode("utf-8", errors="ignore")) > limit:
            return HTMLResponse(_receipt("Too large", "The page HTML exceeded the cap."), status_code=413)
        payload = JobRequest(url=url, title=title, client_html=client_html)
        job_id = _queue(payload)
        return HTMLResponse(_receipt("Queued", f"{title or url}", job_id))

    @app.get("/jobs", dependencies=[Depends(require_token)])
    def list_jobs(
        state: str | None = Query(default=None), limit: int = Query(default=100, le=1000)
    ) -> list[dict[str, Any]]:
        return database.list_jobs(state=state, limit=limit)

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
    def get_job(job_id: str) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        job["stage_times"] = database.stage_times(job_id)
        return job

    @app.post("/jobs/{job_id}/rerender", dependencies=[Depends(require_token)])
    def rerender(
        job_id: str, from_stage: str = Query(default="preparing", alias="from")
    ) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        if from_stage not in STAGES:
            raise HTTPException(
                status_code=400, detail=f"unknown stage; known: {', '.join(STAGES)}"
            )
        fields: dict[str, Any] = {
            "state": "queued",
            "pending_from": from_stage,
            "error": None,
            "stage_failed": None,
            "render_version": int(job.get("render_version") or 1) + 1,
        }
        if from_stage in ("fetching", "extracting", "preparing"):
            # Asking for the rules again is asking to discard the hand edit.
            fields["text_edited"] = 0
        database.update_job(job_id, **fields)
        return {"id": job_id, "queued_from": from_stage}

    @app.delete("/jobs/{job_id}", dependencies=[Depends(require_token)])
    def remove(job_id: str) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        database.update_job(job_id, in_feed=0)
        feed_mod.write_feed(database, cfg)
        return {"id": job_id, "in_feed": False, "files": "kept"}

    # -- the web dashboard ---------------------------------------------------

    @app.get("/api/state", dependencies=[Depends(require_token)])
    def api_state() -> dict[str, Any]:
        """Everything the dashboard draws, in one poll."""
        from readcast.dashboard import chunk_progress, seconds_per_chunk, when
        from readcast.voices import effective_backend, valid_voice

        def summarize(job: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": job["id"],
                "title": job.get("title") or job.get("url"),
                "url": job.get("url"),
                "state": job["state"],
                "words": job.get("word_count"),
                "duration_s": job.get("duration_s"),
                "rtf": job.get("rtf"),
                "flagged": job.get("flagged_chunks") or 0,
                "hold": bool(job.get("hold")),
                "error": job.get("error"),
                "stage_failed": job.get("stage_failed"),
                "voice": valid_voice(cfg, job),
                "backend": effective_backend(cfg, job),
                "editable": (cfg.jobs_dir / str(job["id"]) / "spoken.txt").is_file()
                and job["state"] not in ("synthesizing", "assembling"),
            }

        running = None
        for state in ("synthesizing", "assembling", "fetching", "extracting", "preparing"):
            found = database.list_jobs(state=state, limit=1)
            if found:
                job = found[0]
                progress = chunk_progress(
                    cfg.jobs_dir / str(job["id"]),
                    seconds_per_chunk(database, job.get("backend")),
                )
                running = summarize(job) | {
                    "done": progress.done,
                    "total": progress.total,
                    "eta_s": progress.eta_s,
                    "finish_at": progress.finish_at,
                    "finish_label": when(progress.finish_at),
                    "per_chunk_s": progress.per_chunk,
                    "started": progress.started,
                }
                break

        done = [j for j in database.list_jobs(state="done", limit=10_000)
                if int(j.get("in_feed", 1))]
        return {
            "now": running,
            "ready": [summarize(j) for j in database.ready_queue()],
            "queued": [summarize(j) for j in reversed(database.list_jobs(state="queued", limit=50))],
            "recent": [summarize(j) for j in database.list_jobs(limit=12)],
            "feed_url": feed_mod.feed_url(cfg.base_url, database.feed_token()),
            "episodes": len(done),
            "audio_s": sum(float(j.get("duration_s") or 0) for j in done),
            "failed": len(database.list_jobs(state="failed", limit=10_000)),
            "hold_new": hold_new_jobs(cfg, database),
            "seconds_per_chunk": seconds_per_chunk(
                database, (running or {}).get("backend")
            ),
        }

    @app.post("/api/settings/hold-new", dependencies=[Depends(require_token)])
    def api_hold_new(on: bool = Query(...)) -> dict[str, Any]:
        """Whether newly prepared jobs wait for a look before they render."""
        set_hold_new_jobs(database, on)
        log.info("new jobs will %s", "wait for review" if on else "render automatically")
        return {"hold_new": on}

    @app.post("/api/queue/order", dependencies=[Depends(require_token)])
    def api_reorder(payload: QueueOrder) -> dict[str, Any]:
        return {"order": database.set_queue_order(payload.order)}

    @app.get("/api/jobs/{job_id}/text", dependencies=[Depends(require_token)])
    def api_get_text(job_id: str) -> dict[str, Any]:
        from readcast.dashboard import seconds_per_chunk

        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        path = cfg.jobs_dir / job_id / "spoken.txt"
        text = path.read_text() if path.is_file() else ""

        # What a full re-render would cost, so the button can say so before it
        # spends two hours of the machine.
        plan = cfg.jobs_dir / job_id / "chunks" / "plan.jsonl"
        if plan.is_file():
            chunks = sum(1 for line in plan.read_text().splitlines() if line.strip())
        else:
            chunks = max(1, round(len(text) / int(cfg["chunk"]["target_chars"])))
        return {
            "id": job_id,
            "title": job.get("title"),
            "state": job["state"],
            "hold": bool(job.get("hold")),
            "text": text,
            "exists": path.is_file(),
            "editable": path.is_file()
            and job["state"] not in ("synthesizing", "assembling"),
            "chunks": chunks,
            "rerender_s": chunks * seconds_per_chunk(database, job.get("backend")),
        }

    @app.put("/api/jobs/{job_id}/text", dependencies=[Depends(require_token)])
    def api_put_text(job_id: str, payload: TextEdit) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        if job["state"] in ("synthesizing", "assembling"):
            # The audio lane has already read this file.
            raise HTTPException(
                status_code=409, detail="this job is being synthesized; hold it first"
            )
        path = cfg.jobs_dir / job_id / "spoken.txt"
        if not path.parent.is_dir():
            raise HTTPException(status_code=404, detail="job directory is missing")
        text = payload.text if payload.text.endswith("\n") else payload.text + "\n"
        path.write_text(text)
        words = len(re.findall(r"\b[\w'’-]+\b", text))
        database.update_job(job_id, word_count=words, text_edited=1)
        log.info("%s spoken.txt edited by hand: %d words", job_id, words)
        return {"id": job_id, "words": words, "bytes": len(text.encode())}

    @app.get("/api/voices", dependencies=[Depends(require_token)])
    def api_voices() -> dict[str, Any]:
        from readcast.voices import pitch_of, pool_voices

        return {
            "pool": [
                {"name": p.name, "pitch": pitch_of(p)} for p in pool_voices(cfg)
            ]
        }

    def _voice_name(value: Any) -> str:
        return value.name if hasattr(value, "name") else str(value)

    @app.post("/api/jobs/{job_id}/voice/reroll", dependencies=[Depends(require_token)])
    def api_reroll_voice(job_id: str, name: str | None = Query(default=None)) -> dict[str, Any]:
        """Give this episode a different narrator, or a named one."""
        from readcast.voices import reroll_any

        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        if job["state"] in ("synthesizing", "assembling"):
            raise HTTPException(
                status_code=409, detail="this job is being synthesized; hold it first"
            )
        cast = reroll_any(database, cfg, job, name)
        if cast is None:
            raise HTTPException(
                status_code=409,
                detail="no other voice available for this backend; add some with "
                       "`readcast voices sample` or list more under "
                       "tts.backends.<backend>.voices",
            )
        return {
            "id": job_id,
            "voice": _voice_name(cast["main"]),
            "quote": _voice_name(cast["quote"]),
            "aside": _voice_name(cast["aside"]),
        }

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
    def api_cancel(job_id: str) -> dict[str, Any]:
        """Stop a render, or take a waiting job out of the queue.

        Audio already made is kept, so releasing the job again resumes it
        rather than starting over.
        """
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        state = str(job["state"])
        if state in ("synthesizing", "assembling"):
            # The worker checks this between chunks.
            database.update_job(job_id, cancel_requested=1)
            return {"id": job_id, "stopping": True, "state": state}
        if state in ("queued", "ready", "fetching", "extracting", "preparing"):
            database.update_job(
                job_id, state="cancelled", hold=0, cancel_requested=0,
                error="taken out of the queue",
            )
            return {"id": job_id, "stopping": False, "state": "cancelled"}
        raise HTTPException(
            status_code=409, detail=f"a {state} job is not running or queued"
        )

    @app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
    def api_delete(job_id: str, files: bool = Query(default=False)) -> dict[str, Any]:
        """Remove a job entirely. With files=true, its artifacts go too."""
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        if job["state"] in ("synthesizing", "assembling"):
            raise HTTPException(
                status_code=409, detail="stop the render first, then remove it"
            )
        removed = False
        if files:
            import shutil

            directory = cfg.jobs_dir / job_id
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
                removed = True
        database.delete_job(job_id)
        feed_mod.write_feed(database, cfg)
        log.info("removed %s%s", job_id, " and its files" if removed else "")
        return {"id": job_id, "removed": True, "files_removed": removed}

    @app.post("/api/jobs/{job_id}/backend", dependencies=[Depends(require_token)])
    def api_set_backend(job_id: str, name: str = Query(...)) -> dict[str, Any]:
        """Choose fast or good for one episode.

        Switching invalidates the narrator, because the two kinds of backend do
        not share speakers at all.
        """
        from readcast.synth import backend_names
        from readcast.voices import effective_backend, valid_voice, voices_for_job

        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        if name not in backend_names():
            raise HTTPException(status_code=400, detail=f"unknown backend {name!r}")
        if job["state"] in ("synthesizing", "assembling"):
            raise HTTPException(
                status_code=409, detail="this job is being synthesized; hold it first"
            )
        database.update_job(
            job_id, backend=name,
            voice_main=None, voice_quote=None, voice_aside=None,
        )
        fresh = database.get_job(job_id) or job
        voices_for_job(cfg, database, fresh, effective_backend(cfg, fresh))
        fresh = database.get_job(job_id) or fresh
        return {"id": job_id, "backend": name, "voice": valid_voice(cfg, fresh)}

    @app.get("/api/jobs/{job_id}/voice.wav", dependencies=[Depends(require_token)])
    def api_voice_sample(job_id: str) -> Response:
        """A clip of the narrator, whether it is a recording or a voice pack."""
        from readcast.voices import effective_backend, sample_clip, valid_voice

        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        path = sample_clip(cfg, valid_voice(cfg, job), effective_backend(cfg, job))
        if path is None:
            raise HTTPException(
                status_code=404,
                detail="no sample for this voice; run `readcast voices audition`",
            )
        return Response(
            content=path.read_bytes(), media_type="audio/wav", headers=NOINDEX
        )

    @app.get("/ui", response_class=HTMLResponse)
    def ui() -> HTMLResponse:
        page = files("readcast.web").joinpath("dashboard.html").read_text()
        return HTMLResponse(page)

    # -- status -------------------------------------------------------------

    @app.get("/status", response_class=HTMLResponse)
    def status() -> HTMLResponse:
        jobs = database.list_jobs(limit=50)
        rows = []
        for job in jobs:
            state = str(job["state"])
            detail = html.escape(str(job.get("error") or ""))[:160]
            rows.append(
                "<tr>"
                f"<td class='id'>{html.escape(str(job['id']))}</td>"
                f"<td class='s {state}'>{state}</td>"
                f"<td>{html.escape(str(job.get('title') or job.get('url') or ''))[:90]}</td>"
                f"<td class='n'>{_fmt_duration(job.get('duration_s'))}</td>"
                f"<td class='n'>{_fmt_float(job.get('rtf'))}</td>"
                f"<td class='n'>{job.get('flagged_chunks') or ''}</td>"
                f"<td class='e'>{detail}</td>"
                "</tr>"
            )
        body = f"""<!doctype html><meta charset="utf-8">
<title>readcast status</title>
<style>
 body {{ font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
        margin: 2rem; background: #14161a; color: #e7e9ee; }}
 h1 {{ font-size: 1.1rem; letter-spacing: .04em; text-transform: uppercase; color: #9aa3b2; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #262a33;
          vertical-align: top; }}
 th {{ color: #9aa3b2; font-weight: 500; }}
 td.n {{ text-align: right; }} td.id {{ color: #9aa3b2; }}
 td.e {{ color: #e2727f; }}
 .done {{ color: #7bd88f; }} .failed {{ color: #e2727f; }} .queued {{ color: #9aa3b2; }}
 .fetching, .extracting, .preparing, .synthesizing, .assembling {{ color: #e3c46b; }}
</style>
<h1>readcast — last {len(jobs)} jobs</h1>
<table><tr><th>id</th><th>state</th><th>title</th><th>length</th><th>rtf</th>
<th>flagged</th><th>error</th></tr>
{"".join(rows) or "<tr><td colspan=7>no jobs yet</td></tr>"}</table>"""
        return HTMLResponse(body, headers=NOINDEX)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        def lane(w: Any) -> str:
            if w is None:
                return "off"
            return "running" if w.holds_lock else "waiting for lock"

        return {
            "ok": True,
            "worker": lane(app.state.worker),
            "prep_worker": lane(app.state.prep_worker),
        }

    @app.post("/jobs/{job_id}/hold", dependencies=[Depends(require_token)])
    def hold(job_id: str, release: bool = Query(default=False)) -> dict[str, Any]:
        """Keep prepared text out of the audio lane until it is released."""
        if database.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="no such job")
        database.update_job(job_id, hold=0 if release else 1)
        return {"id": job_id, "hold": not release}

    # -- feed ---------------------------------------------------------------

    # Podcast clients issue HEAD before they download, to check size and type.
    @app.api_route("/f/{token}/feed.xml", methods=["GET", "HEAD"])
    def serve_feed(token: str) -> Response:
        check_feed_token(token)
        path = cfg.feed_dir / "feed.xml"
        if not path.is_file():
            feed_mod.write_feed(database, cfg)
        return Response(
            content=path.read_bytes(),
            media_type="application/rss+xml",
            headers=NOINDEX,
        )

    @app.api_route("/f/{token}/cover.jpg", methods=["GET", "HEAD"])
    def serve_cover(token: str) -> Response:
        check_feed_token(token)
        path = cfg.feed_dir / "cover.jpg"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no cover")
        return Response(content=path.read_bytes(), media_type="image/jpeg", headers=NOINDEX)

    @app.api_route("/f/{token}/audio/{filename}", methods=["GET", "HEAD"])
    def serve_audio(token: str, filename: str, request: Request) -> Response:
        check_feed_token(token)
        job_id = filename[:-4] if filename.endswith(".mp3") else filename
        if not re.fullmatch(r"[A-Za-z0-9._-]+", job_id):
            raise HTTPException(status_code=404, detail="not found")
        job = database.get_job(job_id)
        if job is None or not int(job.get("in_feed", 1)):
            raise HTTPException(status_code=404, detail="not found")
        path = feed_mod.episode_path(cfg.data_dir, job_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return ranged_response(
            path, request.headers.get("range"), head=request.method == "HEAD"
        )

    return app


def _receipt(heading: str, detail: str, job_id: str | None = None) -> str:
    """The little page the form-POST tab lands on. It closes itself."""
    extra = f"<p class='id'>{html.escape(job_id)}</p>" if job_id else ""
    return f"""<!doctype html><meta charset="utf-8"><title>readcast</title>
<style>
 body {{ font: 15px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
        background:#14161a; color:#e7e9ee; display:grid; place-content:center;
        height:100vh; margin:0; text-align:center; }}
 h1 {{ font-size:1.2rem; margin:0 0 .4rem; color:#7bd88f; }}
 p {{ margin:.2rem 0; color:#9aa3b2; max-width:34rem; overflow-wrap:anywhere; }}
 .id {{ color:#5c6473; font-size:.85rem; }}
</style>
<h1>readcast: {html.escape(heading).lower()}</h1>
<p>{html.escape(detail)}</p>{extra}
<p class="id">this tab closes itself</p>
<script>setTimeout(()=>{{window.close();}},1600)</script>"""


def _fmt_duration(seconds: Any) -> str:
    if not seconds:
        return ""
    total = int(float(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _fmt_float(value: Any) -> str:
    return f"{float(value):.2f}" if value else ""


def ranged_response(
    path: Path, range_header: str | None, *, head: bool = False
) -> Response:
    """Podcast clients issue range requests. A 200-only server breaks seeking."""
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", **NOINDEX}

    start, end = 0, size - 1
    status = 200
    if range_header:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if m:
            first, last = m.group(1), m.group(2)
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:  # suffix range: the final N bytes
                start = max(size - int(last), 0)
            if start >= size:
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{size}", **headers},
                )
            end = min(end, size - 1)
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = end - start + 1
    headers["Content-Length"] = str(length)

    if head:
        # Same headers, no body: the client only wants the size and the type.
        return Response(status_code=status, media_type=media_type, headers=headers)

    def stream() -> Iterator[bytes]:
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                block = fh.read(min(64 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(stream(), status_code=status, media_type=media_type, headers=headers)


app_singleton: FastAPI | None = None


def get_app() -> FastAPI:  # pragma: no cover - uvicorn entry point
    global app_singleton
    if app_singleton is None:
        app_singleton = create_app()
    return app_singleton
