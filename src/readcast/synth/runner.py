"""Stage 5: synthesis.

One chunk at a time, verified, retried with a new seed when the engine drops
words. Writes chunks/000.wav upward and reports the real-time factor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from readcast.audio import probe_duration
from readcast.chunk import Chunk
from readcast.synth.verify import verify_chunk

log = logging.getLogger("readcast.synth")


class SynthesisCancelled(RuntimeError):
    """The operator stopped this render. Not a failure."""

# A speech server can drop a connection mid-render. Losing a two-hour job to one
# bad chunk is not acceptable, so a failed call is retried before giving up.
TRANSIENT_ATTEMPTS = 3
TRANSIENT_BACKOFF_S = 2.0


def synth_with_retry(backend: Any, text: str, *, attempts: int = TRANSIENT_ATTEMPTS,
                     **kwargs: Any) -> bytes:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return backend.synth(text, **kwargs)
        except Exception as exc:  # noqa: BLE001 - any backend failure is retryable
            last = exc
            if attempt == attempts:
                break
            wait = TRANSIENT_BACKOFF_S * attempt
            log.warning(
                "speech call failed (%s); retrying in %.0fs [%d/%d]",
                str(exc)[:120], wait, attempt, attempts,
            )
            time.sleep(wait)
    raise last if last else RuntimeError("synthesis failed")


def _text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _place(src: Path, dst: Path) -> bool:
    """Hardlink when the filesystem allows it, so reuse costs no extra disk."""
    if dst.exists():
        try:
            if dst.samefile(src):
                return True
        except OSError:
            pass
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        try:
            shutil.copyfile(src, dst)
        except OSError:
            return False
    return True


def _seed_cache(out_dir: Path, cache_dir: Path, voice_ids: dict[str, str]) -> int:
    """Adopt index-addressed audio from an earlier render into the cache.

    The sidecar records `<voice id>\0<text>`; older sidecars hold the text
    alone and are adopted under every configured voice, since the voice that
    produced them is unknown.
    """
    seeded = 0
    for sidecar in sorted(out_dir.glob("[0-9]*.txt")):
        wav = sidecar.with_suffix(".wav")
        if not wav.is_file():
            continue
        body = sidecar.read_text()
        keys = [body] if "\0" in body else [f"{vid}\0{body}" for vid in voice_ids.values()]
        for candidate in keys or [body]:
            target = cache_dir / f"{_text_key(candidate)}.wav"
            if not target.exists() and _place(wav, target):
                seeded += 1
    if seeded:
        log.info("seeded the reuse cache with %d existing chunk(s)", seeded)
    return seeded


@dataclass
class ChunkAudio:
    chunk: Chunk
    path: Path
    duration_s: float
    attempts: int = 1
    cer: float | None = None
    flagged: bool = False


@dataclass
class SynthResult:
    chunks: list[ChunkAudio] = field(default_factory=list)
    audio_seconds: float = 0.0
    synth_seconds: float = 0.0
    flagged_chunks: int = 0
    reused: int = 0
    skipped: int = 0

    @property
    def rtf(self) -> float:
        """audio_seconds / synthesis_seconds. Higher is faster than real time."""
        return self.audio_seconds / self.synth_seconds if self.synth_seconds else 0.0


def synthesize_chunks(
    chunks: Iterable[Chunk],
    backend: Any,
    out_dir: str | Path,
    *,
    voice: str = "default",
    instructions: dict[str, str] | None = None,
    verify: dict[str, Any] | None = None,
    progress: Callable[[int, int], None] | None = None,
    resume: bool = True,
    voices: dict[str, Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SynthResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    instructions = instructions or {}
    verify = verify or {}
    voices = voices or {}
    verify_on = bool(verify.get("enabled", False))
    max_cer = float(verify.get("max_cer", 0.15))
    retries = int(verify.get("retries", 2))
    whisper_model = str(verify.get("whisper_model", "mlx-community/whisper-small-mlx"))
    # Character error rate is meaningless on a one-word heading: a single
    # mis-heard syllable in 11 characters scores 0.45, and whisper is
    # unreliable on a one-second clip anyway. The failure this check exists to
    # catch — a model dropping words — is a long-input problem.
    min_chars = int(verify.get("min_chars", 40))

    chunks = list(chunks)
    result = SynthResult()

    # Publish the real position. Counting chunk files cannot see it: a rerender
    # overwrites 000.wav upward, so the file count sits still while the worker
    # is halfway through the article.
    progress_file = out_dir / "progress.json"
    fresh_stamps: list[float] = []

    def publish(done: int) -> None:
        try:
            progress_file.write_text(json.dumps({
                "done": done,
                "total": len(chunks),
                "reused": result.reused,
                "started": t0_wall,
                "updated": time.time(),
                "stamps": fresh_stamps[-25:],
            }))
        except OSError:
            pass
    # Reuse is keyed by the text, not by the chunk index. Editing one paragraph
    # shifts every index after it; without this, one edit would re-render the
    # rest of the article.
    cache_dir = out_dir / ".cache"
    if resume:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _seed_cache(
            out_dir, cache_dir,
            {kind: v.cache_id for kind, v in voices.items()} or {"main": "none"},
        )
    t0 = time.perf_counter()
    t0_wall = time.time()
    publish(0)

    for chunk in chunks:
        # Checked between chunks, so stopping is prompt without ever leaving a
        # half-written file behind.
        if should_stop and should_stop():
            result.synth_seconds = time.perf_counter() - t0
            raise SynthesisCancelled(
                f"stopped after {len(result.chunks)} of {len(chunks)} chunks"
            )
        path = out_dir / f"{chunk.index:03d}.wav"
        sidecar = out_dir / f"{chunk.index:03d}.txt"
        voice_ref = voices.get(chunk.kind)
        # A reference clip only means something to a backend that accepts one;
        # a named-voice backend takes the speaker in the `voice` field.
        reference = (
            voice_ref.payload()
            if voice_ref and getattr(backend, "supports_reference", False)
            else None
        )
        chunk_voice = (voice_ref.name if voice_ref and voice_ref.name else voice)
        # The voice is part of the identity of the audio: a chunk read by the
        # narrator is not interchangeable with the same words read as a quote.
        key = _text_key(f"{voice_ref.cache_id if voice_ref else 'none'}\0{chunk.text}")

        # A 40-minute article is a two-hour render. If audio for this exact
        # text exists, a restart or an edit must not pay for it twice.
        if resume:
            cached = cache_dir / f"{key}.wav"
            if cached.is_file() and _place(cached, path):
                duration = probe_duration(path)
                if duration > 0:
                    sidecar.write_text(
                        f"{voice_ref.cache_id if voice_ref else 'none'}\0{chunk.text}"
                    )
                    result.audio_seconds += duration
                    result.chunks.append(
                        ChunkAudio(chunk=chunk, path=path, duration_s=duration, attempts=0)
                    )
                    result.reused += 1
                    publish(len(result.chunks))
                    if progress:
                        progress(chunk.index + 1, len(chunks))
                    continue

        instruction = instructions.get(chunk.kind) if getattr(
            backend, "supports_instruction", False
        ) else None

        best: tuple[float, bytes] | None = None
        attempts = 0
        cer: float | None = None
        flagged = False

        if not any(ch.isalnum() for ch in chunk.text):
            # Nothing to say. Some engines crash on punctuation alone, and a
            # two-hour render should not die for a stray bullet.
            log.warning("chunk %03d has nothing speakable (%r); skipping",
                        chunk.index, chunk.text[:20])
            result.skipped += 1
            publish(len(result.chunks))
            continue

        for attempt in range(retries + 1):
            attempts = attempt + 1
            # mlx-audio takes no seed, so a retry is a fresh sample rather than
            # a seeded one. Other backends still get a changed seed.
            seed = None if attempt == 0 else 1000 + attempt
            audio = synth_with_retry(
                backend, chunk.text, voice=chunk_voice, instruction=instruction,
                seed=seed, reference=reference,
            )
            path.write_bytes(audio)
            if not verify_on or len(chunk.text) < min_chars:
                best = (0.0, audio)
                break
            check = verify_chunk(path, chunk.text, model=whisper_model, max_cer=max_cer)
            if not check.available:
                best = (0.0, audio)
                break
            cer = check.cer
            if best is None or check.cer < best[0]:
                best = (check.cer, audio)
            if check.ok:
                break
            log.warning(
                "chunk %03d CER %.3f above %.3f; retrying with a new seed",
                chunk.index, check.cer, max_cer,
            )
        else:
            flagged = True

        if best is not None:
            path.write_bytes(best[1])
            cer = best[0] if verify_on else None
        if flagged:
            result.flagged_chunks += 1
            log.warning("chunk %03d kept at CER %.3f after %d attempts",
                        chunk.index, cer or 0.0, attempts)

        sidecar.write_text(f"{voice_ref.cache_id if voice_ref else 'none'}\0{chunk.text}")
        if resume:
            _place(path, cache_dir / f"{key}.wav")
        duration = probe_duration(path)
        result.audio_seconds += duration
        result.chunks.append(
            ChunkAudio(chunk=chunk, path=path, duration_s=duration,
                       attempts=attempts, cer=cer, flagged=flagged)
        )
        fresh_stamps.append(time.time())
        publish(len(result.chunks))
        if progress:
            progress(chunk.index + 1, len(chunks))

    result.synth_seconds = time.perf_counter() - t0
    return result
