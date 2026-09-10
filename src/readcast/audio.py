"""ffmpeg wrappers. Every audio operation in readcast goes through here."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("readcast.audio")

SAMPLE_RATE = 24000


class AudioError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise AudioError(f"{tool} is not on PATH; install it with `brew install ffmpeg`")


def run(args: list[str], *, stdin: bytes | None = None, capture_stdout: bool = False) -> bytes:
    proc = subprocess.run(
        args,
        input=stdin,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-6:]
        raise AudioError(f"{args[0]} failed: " + " / ".join(tail))
    return proc.stdout or b""


def probe_duration(path: str | Path) -> float:
    out = run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_stdout=True,
    )
    try:
        return float(json.loads(out)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AudioError(f"could not read duration of {path}") from exc


def to_wav_mono(data: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Whatever the backend returned becomes 16-bit mono PCM at one rate.

    Concatenation only works when every part shares a format.
    """
    return run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le",
            "-f", "wav", "pipe:1",
        ],
        stdin=data,
        capture_stdout=True,
    )


def write_silence(path: str | Path, ms: int, *, sample_rate: int = SAMPLE_RATE) -> Path:
    path = Path(path)
    run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
            "-t", f"{max(ms, 0) / 1000:.3f}", "-c:a", "pcm_s16le", str(path),
        ]
    )
    return path


def concat_wavs(paths: list[Path], out: str | Path) -> Path:
    if not paths:
        raise AudioError("nothing to concatenate")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        for p in paths:
            fh.write(f"file '{Path(p).resolve().as_posix()}'\n")
        listing = fh.name
    try:
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", listing,
                "-c:a", "pcm_s16le", str(out),
            ]
        )
    finally:
        Path(listing).unlink(missing_ok=True)
    return Path(out)


def loudnorm(
    src: str | Path,
    out: str | Path,
    *,
    lufs: float = -16.0,
    true_peak: float = -1.5,
    lra: float = 11.0,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    """Two-pass loudnorm to the podcast convention.

    Without it the episode sits at a different volume from every other show in
    the listener's queue.
    """
    filt = f"loudnorm=I={lufs}:TP={true_peak}:LRA={lra}"
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(src),
            "-af", f"{filt}:print_format=json", "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    measured = {}
    text = proc.stderr.decode(errors="replace")
    start = text.rfind("{")
    if start != -1:
        try:
            measured = json.loads(text[start : text.rfind("}") + 1])
        except ValueError:
            measured = {}

    second = filt
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")

    def usable(key: str) -> bool:
        """A clip too short or too quiet to measure reports -inf, which the
        second pass rejects outright."""
        try:
            value = float(measured[key])
        except (KeyError, TypeError, ValueError):
            return False
        return value == value and abs(value) != float("inf") and -99 <= value <= 99

    if all(usable(k) for k in keys):
        second += (
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            ":linear=true"
        )
    else:
        log.warning(
            "loudnorm measurement unusable (audio too short or silent); "
            "falling back to a single pass"
        )
    second += f":print_format=summary"

    run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-af", second, "-ar", str(sample_rate), "-ac", "1",
            "-c:a", "pcm_s16le", str(out),
        ]
    )
    return Path(out)


def encode_mp3(
    src: str | Path,
    out: str | Path,
    *,
    bitrate_kbps: int = 64,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-ac", "1", "-ar", str(sample_rate), "-b:a", f"{bitrate_kbps}k",
            "-codec:a", "libmp3lame", "-write_xing", "1", str(out),
        ]
    )
    return Path(out)


def tone_wav(text_len: int, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A stand-in waveform whose length tracks the text. Used by the test backend."""
    seconds = max(0.3, min(30.0, text_len / 15.0))
    return run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=180:sample_rate={sample_rate}",
            "-t", f"{seconds:.3f}", "-ac", "1", "-c:a", "pcm_s16le",
            "-f", "wav", "pipe:1",
        ],
        capture_stdout=True,
    )
