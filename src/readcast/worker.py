"""The pipeline, and the one worker that runs it.

Each stage writes its output to disk under the job directory and reads only the
previous stage's file. That is what makes `rerender --from preparing` cheap: the
fetch and the extraction are already on disk.

Run exactly one worker. The speech model holds several gigabytes; a second
worker doubles that and slows both jobs.
"""

from __future__ import annotations

import fcntl
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from readcast import feed as feed_mod
from readcast.assemble import assemble, build_intro, ensure_cover
from readcast.chunk import Chunk, plan_chunks, read_plan, write_plan
from readcast.config import Config
from readcast.db import Database, stage_timer
from readcast.extract import extract
from readcast.fetch import FetchError, fetch_bytes, fetch_url, fetch_with_browser, is_pdf
from readcast.prepare.loader import host_of, load_rules
from readcast.prepare.pipeline import prepare_text
from readcast.synth import get_backend
from readcast.synth.runner import SynthesisCancelled, synthesize_chunks

log = logging.getLogger("readcast.worker")

STAGE_ORDER = ("fetching", "extracting", "preparing", "synthesizing", "assembling")


def _extract_pdf(pdf_path: Path, ruleset: Any, min_chars: int) -> Any:
    from readcast.extract import Extracted
    from readcast.pdf import read_pdf

    doc = read_pdf(pdf_path.read_bytes(), ruleset.extract)
    result = Extracted(markdown=doc.text, title=doc.title, author=doc.author)
    if len(result) < min_chars:
        raise StageError(
            "extracting",
            "the PDF produced too little text; it may be scanned images rather "
            "than a text layer",
        )
    return result


def _extract_html(
    raw_path: Path, url: str, ruleset: Any, cfg: Config, job: dict[str, Any],
    min_chars: int, say: Any,
) -> tuple[Any, str]:
    from readcast.extract import extract

    html_text = raw_path.read_text(errors="replace")
    result = extract(html_text, url, ruleset.extract)
    if len(result) < min_chars and not int(job.get("client_html") or 0):
        say("extracting", "too little text; retrying with a browser")
        try:
            html_text = fetch_with_browser(
                url,
                timeout_s=float(cfg["fetch"]["timeout_s"]),
                user_agent=str(cfg["fetch"]["user_agent"]),
            )
            raw_path.write_text(html_text)
            result = extract(html_text, url, ruleset.extract)
        except FetchError as exc:
            say("extracting", f"browser fetch unavailable: {exc}")
    return result, html_text


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def job_dir(cfg: Config, job_id: str) -> Path:
    path = cfg.jobs_dir / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class RunOptions:
    from_stage: str = "fetching"
    until_stage: str | None = None   # inclusive; stop after this stage
    force_prepare: bool = False      # overwrite text a human edited
    preview_minutes: float | None = None
    backend: str | None = None
    voice: str | None = None
    verify: bool | None = None
    publish: bool = True
    out_dir: Path | None = None


def _stages_from(start: str, until: str | None = None) -> list[str]:
    if start not in STAGE_ORDER:
        raise ValueError(f"unknown stage {start!r}; known: {', '.join(STAGE_ORDER)}")
    stages = list(STAGE_ORDER[STAGE_ORDER.index(start) :])
    if until:
        if until not in STAGE_ORDER:
            raise ValueError(f"unknown stage {until!r}")
        stages = [s for s in stages if STAGE_ORDER.index(s) <= STAGE_ORDER.index(until)]
    return stages


def run_job(
    db: Database,
    cfg: Config,
    job_id: str,
    options: RunOptions | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Run one job from a stage onward. Artifacts already on disk are reused."""
    options = options or RunOptions()
    job = db.get_job(job_id)
    if job is None:
        raise ValueError(f"no such job: {job_id}")

    directory = job_dir(cfg, job_id)
    original_state = str(job["state"])
    stages = _stages_from(options.from_stage, options.until_stage)
    url = str(job["url"])
    ruleset = load_rules(cfg.rules_dir, url)

    backend_name = options.backend or job.get("backend") or cfg["tts"]["backend"]
    voice = options.voice or job.get("voice") or cfg["tts"].get("voice", "default")

    def say(stage: str, message: str) -> None:
        log.info("[%s] %s: %s", job_id, stage, message)
        if progress:
            progress(stage, message)

    try:
        # -- fetch ----------------------------------------------------------
        raw_path = directory / "raw.html"
        pdf_path = directory / "raw.pdf"
        if "fetching" in stages:
            db.update_job(job_id, state="fetching")
            with stage_timer(db, job_id, "fetching"):
                from readcast.extract import looks_like_pdf

                # Chrome renders a PDF in its own viewer, so what the extension
                # captured is an empty shell. Fetch the document itself.
                if (
                    int(job.get("client_html") or 0)
                    and raw_path.is_file()
                    and looks_like_pdf(raw_path.read_text(errors="replace"), url)
                ):
                    say("fetching", "that page is a PDF viewer; fetching the PDF itself")
                    # The title came from the same discarded viewer — it is the
                    # browser's tab title, not the paper's. Let the PDF say.
                    db.update_job(job_id, client_html=0, title=None, title_locked=0)
                    job = db.get_job(job_id) or job

                if int(job.get("client_html") or 0) and raw_path.is_file():
                    say("fetching", "using client_html from the browser")
                elif pdf_path.is_file() and "extracting" not in stages:
                    pass
                else:
                    fetch_cfg = cfg["fetch"]
                    say("fetching", url)
                    try:
                        body, content_type = fetch_bytes(
                            url,
                            timeout_s=float(fetch_cfg["timeout_s"]),
                            user_agent=str(fetch_cfg["user_agent"]),
                        )
                    except FetchError as exc:
                        raise StageError("fetching", str(exc)) from exc
                    if is_pdf(body, content_type, url):
                        pdf_path.write_bytes(body)
                        raw_path.unlink(missing_ok=True)
                        say("fetching", f"PDF, {len(body) // 1024} KB")
                    else:
                        raw_path.write_text(body.decode("utf-8", errors="replace"))
        if not raw_path.is_file() and not pdf_path.is_file():
            raise StageError(
                "fetching", "nothing fetched; run from an earlier stage"
            )

        # -- extract --------------------------------------------------------
        md_path = directory / "extracted.md"
        if "extracting" in stages:
            db.update_job(job_id, state="extracting")
            with stage_timer(db, job_id, "extracting"):
                min_chars = int(cfg["fetch"]["min_extracted_chars"])
                if pdf_path.is_file():
                    result = _extract_pdf(pdf_path, ruleset, min_chars)
                    say("extracting", f"{len(result)} characters from the PDF")
                else:
                    result, html_text = _extract_html(
                        raw_path, url, ruleset, cfg, job, min_chars, say
                    )
                    if len(result) < min_chars:
                        from readcast.extract import looks_like_pdf

                        if looks_like_pdf(html_text, url):
                            raise StageError(
                                "extracting",
                                "this looks like a PDF, and the page captured by "
                                "the browser is only its viewer. Submit the URL "
                                "without the browser extension so readcast can "
                                "fetch the document itself.",
                            )
                        raise StageError(
                            "extracting", "extraction produced too little text"
                        )
                md_path.write_text(result.markdown)
                # Re-extracting is how a wrong title gets corrected, so what
                # extraction finds wins — unless the submitter chose one.
                keep_title = bool(job.get("title_locked")) and job.get("title")
                db.update_job(
                    job_id,
                    title=job.get("title") if keep_title else (
                        result.title or job.get("title")
                    ),
                    author=result.author or job.get("author"),
                    publication=result.publication or job.get("publication"),
                    published_at=result.published_at or job.get("published_at"),
                )
                job = db.get_job(job_id) or job

        if not md_path.is_file():
            raise StageError("extracting", "extracted.md is missing")

        # -- prepare --------------------------------------------------------
        spoken_path = directory / "spoken.txt"
        hand_edited = bool(job.get("text_edited")) and spoken_path.is_file()
        if hand_edited and not options.force_prepare:
            # Rules output is reproducible; the operator's typing is not. Keep
            # the edit, and put the regenerated text beside it to compare.
            if "preparing" in stages:
                say("preparing", "spoken.txt was edited by hand; keeping it")
                stages = [s for s in stages if s != "preparing"]
        if "preparing" in stages:
            db.update_job(job_id, state="preparing")
            with stage_timer(db, job_id, "preparing"):
                backend_probe = get_backend(backend_name, cfg.backend_settings(backend_name))
                prepared = prepare_text(
                    md_path.read_text(),
                    ruleset,
                    host=host_of(url),
                    supports_phonemes=getattr(backend_probe, "supports_phonemes", False),
                )
                prepared.write(directory)
                db.update_job(job_id, word_count=prepared.word_count, text_edited=0)
                say(
                    "preparing",
                    f"{prepared.word_count} words, {len(prepared.transforms)} transforms, "
                    f"{len(prepared.unknowns)} unknown terms",
                )
        if not spoken_path.is_file():
            raise StageError("preparing", "spoken.txt is missing")

        if options.until_stage == "preparing":
            # The text is on disk. The expensive half waits for the audio lane.
            from readcast.config import hold_new_jobs

            hold = 1 if hold_new_jobs(cfg, db) else 0
            fields: dict[str, Any] = {
                "state": "ready", "stage_failed": None, "error": None, "hold": hold,
            }
            if job.get("queue_rank") is None:
                fields["queue_rank"] = db.next_queue_rank()  # joins the back of the queue
            db.update_job(job_id, **fields)
            # Cast it now rather than at synthesis, so the operator can hear and
            # change the narrator while the job is still waiting.
            from readcast.voices import effective_backend, voices_for_job

            if str(cfg["tts"].get("voice_mode", "per_episode")) == "per_episode":
                fresh = db.get_job(job_id) or job
                voices_for_job(cfg, db, fresh, effective_backend(cfg, fresh))
            say("preparing", "ready — text on disk, waiting for the audio lane")
            return db.get_job(job_id) or job

        job = db.get_job(job_id) or job
        meta = {
            "title": job.get("title"),
            "author": job.get("author"),
            "publication": job.get("publication"),
            "published_at": job.get("published_at"),
            "url": url,
        }

        # -- synthesize -----------------------------------------------------
        work_dir = options.out_dir or directory
        chunks_dir = Path(work_dir) / "chunks"
        rtf = job.get("rtf")
        if "synthesizing" in stages:
            db.update_job(job_id, state="synthesizing", backend=backend_name, voice=voice)
            with stage_timer(db, job_id, "synthesizing"):
                intro = build_intro(str(cfg["feed"]["intro_template"]), meta)
                chunks = plan_chunks(
                    spoken_path.read_text(),
                    target_chars=int(cfg["chunk"]["target_chars"]),
                    max_chars=int(cfg["chunk"]["max_chars"]),
                    intro=intro or None,
                )
                if options.preview_minutes:
                    budget = options.preview_minutes * 60 * 15  # ~15 chars per second
                    kept, used = [], 0
                    for chunk in chunks:
                        if used > budget:
                            break
                        kept.append(chunk)
                        used += len(chunk.text)
                    chunks = kept
                # Adopt audio produced before per-chunk sidecars existed, so a
                # long render interrupted by a restart resumes instead of
                # starting over.
                _adopt_existing_audio(chunks_dir, read_plan(work_dir), chunks)
                write_plan(chunks, work_dir)

                verify_cfg = dict(cfg["verify"])
                if options.verify is not None:
                    verify_cfg["enabled"] = options.verify
                backend = get_backend(backend_name, cfg.backend_settings(backend_name))
                from readcast.voices import voices_for_job

                voices = voices_for_job(cfg, db, job, backend_name)
                missing = sorted(
                    {v.role for v in voices.values() if not (v.available or v.name)}
                )
                if missing:
                    log.warning(
                        "no reference clip for %s; the model will sample a new "
                        "speaker for every chunk. Run `readcast voices sample`.",
                        ", ".join(missing),
                    )
                else:
                    by_role = {
                        v.role: (v.name or v.audio.name) for v in voices.values()
                    }
                    say("synthesizing",
                        "voices " + ", ".join(f"{r}={n}" for r, n in by_role.items()))
                say("synthesizing", f"{len(chunks)} chunks on {backend_name}")
                synth = synthesize_chunks(
                    chunks,
                    backend,
                    chunks_dir,
                    voice=voice,
                    instructions=dict(cfg["tts"].get("instructions") or {}),
                    voices=voices,
                    should_stop=lambda: bool(
                        (db.get_job(job_id) or {}).get("cancel_requested")
                    ),
                    verify=verify_cfg,
                    progress=lambda done, total: (
                        say("synthesizing", f"chunk {done}/{total}")
                        if done % 10 == 0 or done == total
                        else None
                    ),
                )
                rtf = synth.rtf
                db.update_job(
                    job_id, rtf=round(synth.rtf, 3), flagged_chunks=synth.flagged_chunks
                )
                # Teach the estimator what this machine actually does. Reused
                # chunks cost nothing, so they would flatter the number.
                from readcast.dashboard import record_chunk_rate

                record_chunk_rate(
                    db, synth.synth_seconds, len(synth.chunks) - synth.reused,
                    backend=backend_name,
                )
                say(
                    "synthesizing",
                    f"{synth.audio_seconds:.0f}s of audio in {synth.synth_seconds:.0f}s "
                    f"(rtf {synth.rtf:.2f}), {synth.flagged_chunks} flagged",
                )
                synth_chunks = synth.chunks
        else:
            from readcast.audio import probe_duration
            from readcast.synth.runner import ChunkAudio

            synth_chunks = []
            for chunk in read_plan(work_dir):
                path = chunks_dir / f"{chunk.index:03d}.wav"
                if not path.is_file():
                    raise StageError("synthesizing", f"missing chunk audio {path.name}")
                synth_chunks.append(
                    ChunkAudio(chunk=chunk, path=path, duration_s=probe_duration(path))
                )

        # -- assemble -------------------------------------------------------
        if "assembling" in stages:
            db.update_job(job_id, state="assembling")
            with stage_timer(db, job_id, "assembling"):
                out_path = Path(work_dir) / "episode.mp3"
                cover = ensure_cover(cfg.feed_dir / "cover.jpg", str(cfg["feed"]["title"]))
                built = assemble(
                    synth_chunks,
                    out_path,
                    meta=meta,
                    audio_cfg=dict(cfg["audio"]),
                    cover=cover,
                )
                say(
                    "assembling",
                    f"{built.duration_s:.0f}s, {built.bytes} bytes, "
                    f"{len(built.chapters)} chapters",
                )
                if options.publish:
                    db.update_job(
                        job_id,
                        duration_s=round(built.duration_s, 3),
                        bytes=built.bytes,
                        state="done",
                        stage_failed=None,
                        error=None,
                    )

        if options.publish:
            db.update_job(job_id, state="done", stage_failed=None, error=None)
            feed_mod.write_feed(db, cfg)
        else:
            # A preview or a comparison leaves the job row as it found it.
            db.update_job(job_id, state=original_state)
        return db.get_job(job_id) or job

    except SynthesisCancelled as exc:
        # Deliberate, so it is not a failure: keep the audio already made, and
        # let a release pick the job up again where it stopped.
        log.info("[%s] %s", job_id, exc)
        db.update_job(
            job_id, state="cancelled", cancel_requested=0,
            stage_failed=None, error=str(exc),
        )
        return db.get_job(job_id) or job
    except StageError as exc:
        log.error("[%s] failed at %s: %s", job_id, exc.stage, exc)
        db.update_job(job_id, state="failed", stage_failed=exc.stage, error=str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - any stage failure is a job failure
        stage = db.get_job(job_id).get("state") if db.get_job(job_id) else "unknown"
        log.exception("[%s] failed during %s", job_id, stage)
        db.update_job(job_id, state="failed", stage_failed=stage, error=str(exc))
        raise


class Worker:
    """The audio lane. One at a time: the speech model holds several gigabytes.

    A file lock keeps a second process from starting one.
    """

    lock_name = "worker.lock"
    claim_states = ("synthesizing", "assembling")
    label = "audio"

    def __init__(self, db: Database, cfg: Config, poll_seconds: float = 1.0):
        self.db = db
        self.cfg = cfg
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_file = None

    def _acquire_lock(self) -> bool:
        self.cfg.ensure_dirs()
        path = self.cfg.data_dir / self.lock_name
        self._lock_file = path.open("w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            return False
        self._lock_file.write(str(time.time()))
        self._lock_file.flush()
        return True

    def reap_orphans(self) -> list[str]:
        """Fail any job left mid-flight by a crash or a restart.

        We hold the only worker lock, so nothing else can be running a job. A
        row still sitting in a running state is from a process that died. Its
        artifacts are on disk, so `rerender --from <stage>` resumes cheaply.
        """
        orphans = []
        for state in self.claim_states:
            for job in self.db.list_jobs(state=state, limit=1000):
                job_id = str(job["id"])
                self.db.update_job(
                    job_id,
                    state="failed",
                    stage_failed=state,
                    error=f"interrupted during {state}; the worker restarted",
                )
                orphans.append(job_id)
                log.warning(
                    "job %s was interrupted during %s; marked failed. "
                    "Resume it with: readcast rerender %s --from %s",
                    job_id, state, job_id, state,
                )
        return orphans

    @property
    def holds_lock(self) -> bool:
        return self._lock_file is not None

    def start(self, wait_for_lock: bool = False) -> bool:
        """Take the lane's lock and start the loop.

        `wait_for_lock` keeps trying in the background. A service restart races
        its own predecessor — the old process can still hold the lock for a few
        seconds — and a server that gave up permanently would sit there serving
        HTTP while nothing ever ran.
        """
        if not self._acquire_lock():
            if not wait_for_lock:
                log.warning(
                    "another readcast %s worker holds the lock; not starting a second one",
                    self.label,
                )
                return False
            log.warning(
                "the %s lock is held; waiting for it to free up", self.label
            )
        else:
            self.reap_orphans()
        self._thread = threading.Thread(
            target=self._loop, name=f"readcast-{self.label}", daemon=True
        )
        self._thread.start()
        log.info("%s worker started", self.label)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._lock_file:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def cast_waiting_jobs(self) -> int:
        """Give every waiting job a narrator, so the queue can show one.

        Casting is free — it picks a name or a clip, it synthesizes nothing —
        and a job that changed backend has had its old cast cleared.
        """
        from readcast.voices import effective_backend, voices_for_job

        if str(self.cfg["tts"].get("voice_mode", "per_episode")) != "per_episode":
            return 0
        cast = 0
        for job in self.db.list_jobs(state="ready", limit=200):
            if job.get("voice_main"):
                continue
            try:
                voices_for_job(self.cfg, self.db, job, effective_backend(self.cfg, job))
                cast += 1
            except Exception:  # noqa: BLE001 - a missing pool must not stop the lane
                log.debug("could not cast %s yet", job["id"])
        return cast

    def claim(self) -> dict[str, Any] | None:
        self.cast_waiting_jobs()
        return self.db.claim_next_ready()

    def options_for(self, job: dict[str, Any]) -> RunOptions:
        start = str(job.get("pending_from") or "synthesizing")
        if start not in STAGE_ORDER:
            start = "synthesizing"
        return RunOptions(from_stage=start)

    def run_once(self) -> str | None:
        job = self.claim()
        if job is None:
            return None
        job_id = str(job["id"])
        options = self.options_for(job)
        self.db.update_job(job_id, pending_from=None)
        try:
            run_job(self.db, self.cfg, job_id, options)
        except Exception:  # noqa: BLE001 - the job row already records the failure
            pass
        return job_id

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.holds_lock:
                    if not self._acquire_lock():
                        self._stop.wait(2.0)
                        continue
                    log.info("%s worker acquired the lock", self.label)
                    self.reap_orphans()
                if self.run_once() is None:
                    self._stop.wait(self.poll_seconds)
            except Exception:  # noqa: BLE001 - the loop must survive a bad job
                log.exception("worker loop error")
                self._stop.wait(self.poll_seconds)


def _adopt_existing_audio(
    chunks_dir: Path, previous: list[Chunk], current: list[Chunk]
) -> int:
    """Write the resume sidecar for chunk audio that already matches the plan."""
    if not previous or not chunks_dir.is_dir():
        return 0
    by_index = {c.index: c for c in previous}
    adopted = 0
    for chunk in current:
        old = by_index.get(chunk.index)
        if old is None or old.text != chunk.text:
            continue
        wav = chunks_dir / f"{chunk.index:03d}.wav"
        sidecar = chunks_dir / f"{chunk.index:03d}.txt"
        if wav.is_file() and not sidecar.is_file():
            sidecar.write_text(chunk.text)
            adopted += 1
    if adopted:
        log.info("adopted %d chunk(s) of existing audio for resume", adopted)
    return adopted


def write_job_json(directory: Path, payload: dict[str, Any]) -> None:
    (directory / "job.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))


class PrepWorker(Worker):
    """The text lane: fetch, extract, prepare.

    Runs alongside the audio worker on purpose. Preparation is seconds of CPU
    and touches no model, so making it wait behind a two-hour render would mean
    the operator cannot see the text until long after submitting it. Finishing
    here leaves the job in `ready` with spoken.txt on disk.
    """

    lock_name = "prep.lock"
    claim_states = ("fetching", "extracting", "preparing")
    label = "prep"

    def claim(self) -> dict[str, Any] | None:
        return self.db.claim_next_queued()

    def options_for(self, job: dict[str, Any]) -> RunOptions:
        start = str(job.get("pending_from") or "fetching")
        if start not in ("fetching", "extracting", "preparing"):
            start = "fetching"
        return RunOptions(from_stage=start, until_stage="preparing")
