"""Voice identity.

Breeze TTS 2 samples a speaker every time it generates. Ask it for 184 chunks
and you get something close to 184 different readers — measured on one episode:
pitch from 103 to 353 Hz, with 43 of 59 consecutive chunks jumping more than
25 Hz.

The fix is to condition every request on a reference clip. One clip per role
gives a stable narrator, a stable quoting voice, and a stable voice for asides.
"""

from __future__ import annotations

import json
import logging
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("readcast.voices")

DEFAULT_ROLES = {
    "intro": "main",
    "heading": "main",
    "body": "main",
    "quote": "quote",
    "aside": "aside",
}

# Spoken once to make a reference clip. Long enough to carry a voice, short
# enough to synthesize while the operator waits. It makes no claim about the
# feed: a narrator is fixed for one episode, not for the show.
REFERENCE_TEXT = (
    "This is the reading voice for this episode. It stays the same from the "
    "first paragraph to the last, so the listener is never left wondering who "
    "is speaking."
)


@dataclass
class Voice:
    role: str
    audio: Path | None = None
    text: str = REFERENCE_TEXT
    #: For backends whose speakers are named voice packs rather than clips.
    name: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.audio and self.audio.is_file())

    def resolved_text(self) -> str:
        """The words actually spoken in this clip.

        Conditioning needs the transcript to match the audio, so each clip
        carries its own. Falling back to the configured default is only correct
        for clips recorded under it.
        """
        if self.audio:
            sidecar = self.audio.with_suffix(".txt")
            if sidecar.is_file():
                return sidecar.read_text().strip()
        return self.text

    @property
    def cache_id(self) -> str:
        """Part of the chunk reuse key: a new voice must not reuse old audio."""
        if self.name:
            return f"{self.role}:{self.name}"
        if not self.available:
            return f"{self.role}:none"
        stat = self.audio.stat()
        return f"{self.role}:{stat.st_size}:{int(stat.st_mtime)}"

    def payload(self) -> dict[str, Any]:
        if self.name or not self.available:
            return {}   # a named voice travels in the `voice` field instead
        return {"ref_audio": str(self.audio.resolve()), "ref_text": self.resolved_text()}


def _entry(value: Any, role: str, root: Path) -> Voice:
    if isinstance(value, dict):
        audio = value.get("audio") or value.get("ref_audio")
        text = value.get("text") or value.get("ref_text") or REFERENCE_TEXT
    else:
        audio, text = value, REFERENCE_TEXT
    if not audio:
        return Voice(role=role)
    path = Path(str(audio)).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    return Voice(role=role, audio=path, text=str(text))


def load_voices(cfg: Any) -> dict[str, Voice]:
    """Map chunk kind to the voice that reads it."""
    tts = cfg["tts"]
    configured = dict(tts.get("voices") or {})
    roles = {**DEFAULT_ROLES, **(tts.get("roles") or {})}
    shared_text = str(tts.get("reference_text") or REFERENCE_TEXT)

    voices: dict[str, Voice] = {}
    for role, value in configured.items():
        voice = _entry(value, role, cfg.root)
        if voice.text == REFERENCE_TEXT:
            voice.text = shared_text
        voices[role] = voice

    out: dict[str, Voice] = {}
    for kind, role in roles.items():
        voice = voices.get(role)
        if voice is None:
            voice = voices.get("main") or Voice(role=role, text=shared_text)
        out[kind] = voice
    return out


# -- a cast per episode -----------------------------------------------------
#
# One voice throughout an article; a different one next article. The switching
# that grates is switching mid-piece, not variety between pieces.

CAST_ROLES = ("main", "quote", "aside")


def pool_dir(cfg: Any) -> Path:
    configured = cfg["tts"].get("voice_pool") or "./data/voices/pool"
    path = Path(str(configured)).expanduser()
    if not path.is_absolute():
        path = (cfg.root / path).resolve()
    return path


def pool_voices(cfg: Any) -> list[Path]:
    return sorted(p for p in pool_dir(cfg).glob("*.wav") if p.is_file())


def effective_backend(cfg: Any, job: dict[str, Any] | None = None) -> str:
    """The backend a job will actually use: its own, else the configured default."""
    if job and job.get("backend"):
        return str(job["backend"])
    return str(cfg["tts"]["backend"])


def sample_clip(cfg: Any, voice: str | None, backend: str | None = None) -> Path | None:
    """A wav to play for a voice, whether it is a clip or a voice-pack name.

    Named voices have no clip of their own, so `voices audition` renders one per
    name and they are served from there.
    """
    if not voice:
        return None
    if voice.endswith(".wav"):
        path = pool_dir(cfg) / voice
        return path if path.is_file() else None
    audition = cfg.data_dir / "voices" / "audition" / f"{voice}.wav"
    return audition if audition.is_file() else None


def named_pool(cfg: Any, backend: str | None) -> list[str]:
    """Voice-pack names for a backend whose speakers are named, not recorded."""
    if not backend:
        return []
    settings = cfg.backend_settings(backend)
    return [str(n) for n in (settings.get("voices") or [])]


def _named_cast(db: Any, names: list[str], current: str | None = None,
                random_pick: bool = False) -> dict[str, str]:
    import secrets

    options = [n for n in names if n != current] or names
    if random_pick:
        main = secrets.choice(options)
    else:
        recent = _recent_mains(db)
        rank = {n: len(recent) for n in options}
        for position, name in enumerate(recent):
            if name in rank:
                rank[name] = min(rank[name], position)
        main = max(options, key=lambda n: (rank[n], n))
    rest = [n for n in names if n != main]
    return {
        "main": main,
        "quote": rest[0] if rest else main,
        "aside": rest[1] if len(rest) > 1 else (rest[0] if rest else main),
    }


def _recent_mains(db: Any, limit: int = 50) -> list[str]:
    """Which narrator each recent episode used, newest first."""
    return [
        str(job["voice_main"])
        for job in db.list_jobs(limit=limit)
        if job.get("voice_main")
    ]


def least_recently_used(pool: list[Path], recent: list[str]) -> Path | None:
    """The narrator that has not read for the longest.

    Least recently used rather than random: random repeats, and two identical
    narrators in a row is the thing the operator noticed in the first place.
    """
    if not pool:
        return None
    by_name = {p.name: p for p in pool}
    used_order = [name for name in recent if name in by_name]
    freshness = {p.name: len(used_order) for p in pool}
    for position, name in enumerate(used_order):
        freshness[name] = min(freshness.get(name, position), position)
    return max(pool, key=lambda p: (freshness.get(p.name, len(used_order)), p.name))


def cast_around(pool: list[Path], main: Path) -> dict[str, Path]:
    """Given a narrator, pick the quoting and aside voices least like it."""
    pitches = {p: pitch_of(p) for p in pool}
    main_hz = pitches.get(main)
    others = [p for p in pool if p != main]
    if main_hz and others:
        others.sort(key=lambda p: -abs((pitches.get(p) or main_hz) - main_hz))
    cast = {"main": main}
    cast["quote"] = others[0] if others else main
    cast["aside"] = others[1] if len(others) > 1 else cast["quote"]
    return cast


def choose_cast(pool: list[Path], recent: list[str]) -> dict[str, Path] | None:
    main = least_recently_used(pool, recent)
    return cast_around(pool, main) if main else None


def save_cast(db: Any, job_id: str, cast: dict[str, Path]) -> None:
    db.update_job(job_id, **{f"voice_{r}": cast[r].name for r in CAST_ROLES})


def reroll_named(db: Any, cfg: Any, job: dict[str, Any], backend: str,
                 name: str | None = None) -> dict[str, str] | None:
    names = named_pool(cfg, backend)
    if not names or (name and name not in names):
        return None
    current = job.get("voice_main")
    if name:
        rest = [n for n in names if n != name]
        cast = {"main": name,
                "quote": rest[0] if rest else name,
                "aside": rest[1] if len(rest) > 1 else (rest[0] if rest else name)}
    else:
        if len([n for n in names if n != current]) == 0:
            return None
        cast = _named_cast(db, names, current=current, random_pick=True)
    db.update_job(str(job["id"]), **{f"voice_{r}": cast[r] for r in CAST_ROLES})
    return cast


def reroll_any(db: Any, cfg: Any, job: dict[str, Any],
               name: str | None = None) -> dict[str, Any] | None:
    """Re-roll using whichever kind of speaker this job's backend takes."""
    backend = effective_backend(cfg, job)
    if named_pool(cfg, backend):
        return reroll_named(db, cfg, job, backend, name=name)
    if name:
        return set_cast(db, cfg, job, name)
    return reroll_cast(db, cfg, job)


def valid_voice(cfg: Any, job: dict[str, Any]) -> str | None:
    """The stored narrator, if it still means something for this backend.

    Switching backends leaves a Breeze clip name on a job that will be read by a
    voice pack; showing it would be a lie.
    """
    voice = job.get("voice_main")
    if not voice:
        return None
    backend = effective_backend(cfg, job)
    names = named_pool(cfg, backend)
    if names:
        return str(voice) if voice in names else None
    return str(voice) if (pool_dir(cfg) / str(voice)).is_file() else None


def reroll_cast(db: Any, cfg: Any, job: dict[str, Any]) -> dict[str, Path] | None:
    """Pick a different narrator at random, and keep it.

    Random, not least-recently-used: pressing the button is the operator asking
    to hear something else, and an ordered walk through the pool feels like a
    toggle between two voices. Automatic casting still uses the ordered pick, so
    consecutive episodes never match.
    """
    import secrets

    pool = pool_voices(cfg)
    current = job.get("voice_main")
    others = [p for p in pool if p.name != current]
    if not others:
        return None
    main = secrets.choice(others)
    cast = cast_around(pool, main)
    save_cast(db, str(job["id"]), cast)
    log.info("%s re-cast: main=%s", job["id"], cast["main"].name)
    return cast


def set_cast(db: Any, cfg: Any, job: dict[str, Any], name: str) -> dict[str, Path] | None:
    """Pin a specific narrator by pool file name."""
    pool = pool_voices(cfg)
    chosen = next((p for p in pool if p.name == name), None)
    if chosen is None:
        return None
    cast = cast_around(pool, chosen)
    save_cast(db, str(job["id"]), cast)
    return cast


def assign_cast(db: Any, cfg: Any, job: dict[str, Any]) -> dict[str, Path] | None:
    """Give a job its cast once, and keep it. A rerender must sound the same."""
    pool = pool_voices(cfg)
    existing = {
        role: pool_dir(cfg) / str(job.get(f"voice_{role}"))
        for role in CAST_ROLES
        if job.get(f"voice_{role}")
    }
    if existing and all(p.is_file() for p in existing.values()):
        return existing
    cast = choose_cast(pool, _recent_mains(db))
    if not cast:
        return None
    save_cast(db, str(job["id"]), cast)
    log.info(
        "%s cast: %s",
        job["id"], ", ".join(f"{r}={cast[r].name}" for r in CAST_ROLES),
    )
    return cast


def voices_for_job(
    cfg: Any, db: Any, job: dict[str, Any], backend: str | None = None
) -> dict[str, Voice]:
    """Map chunk kind to voice, per episode or fixed, depending on config."""
    tts = cfg["tts"]
    roles = {**DEFAULT_ROLES, **(tts.get("roles") or {})}
    text = str(tts.get("reference_text") or REFERENCE_TEXT)
    per_episode = str(tts.get("voice_mode", "per_episode")) == "per_episode"

    names = named_pool(cfg, backend)
    if names and per_episode:
        # A named-voice backend never wanders, so variety means a different
        # name per episode rather than a recorded clip.
        stored = {r: job.get(f"voice_{r}") for r in CAST_ROLES}
        cast = (
            {r: str(v) for r, v in stored.items()}
            if all(stored.get(r) in names for r in CAST_ROLES)
            else _named_cast(db, names)
        )
        if cast != stored:
            db.update_job(str(job["id"]), **{f"voice_{r}": cast[r] for r in CAST_ROLES})
        return {kind: Voice(role=role, name=cast.get(role, cast["main"]), text=text)
                for kind, role in roles.items()}

    if per_episode:
        cast = assign_cast(db, cfg, job)
        if cast:
            return {
                kind: Voice(role=role, audio=cast.get(role, cast["main"]), text=text)
                for kind, role in roles.items()
            }
        log.warning(
            "no voices in %s; the model will sample a new speaker for every "
            "chunk. Run `readcast voices sample`.", pool_dir(cfg)
        )
    return load_voices(cfg)


def voice_dir(cfg: Any) -> Path:
    path = cfg.data_dir / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- picking a voice by ear, with a number to go on -------------------------


def estimate_pitch(path: str | Path, floor_hz: int = 70, ceil_hz: int = 350) -> float | None:
    """Rough fundamental frequency of the loudest voiced second.

    Only used to tell candidate voices apart and to check that a render kept one
    voice throughout. Autocorrelation is enough for that and needs no library.
    """
    try:
        with wave.open(str(path)) as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            raw = handle.readframes(frames)
    except (wave.Error, OSError):
        return None
    if frames < rate // 2:
        return None

    samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])
    window = min(rate, len(samples))
    best_start, best_energy = 0, -1.0
    for start in range(0, max(len(samples) - window, 1), max(window // 2, 1)):
        energy = sum(abs(s) for s in samples[start : start + window]) / window
        if energy > best_energy:
            best_energy, best_start = energy, start

    segment = samples[best_start : best_start + window]
    if not segment:
        return None
    mean = sum(segment) / len(segment)
    segment = [s - mean for s in segment]

    lo, hi = rate // ceil_hz, rate // floor_hz
    best_lag, best_score = 0, 0.0
    for lag in range(lo, min(hi, len(segment) - 1)):
        score = sum(segment[i] * segment[i + lag] for i in range(0, len(segment) - lag, 4))
        if score > best_score:
            best_score, best_lag = score, lag
    return rate / best_lag if best_lag else None


# A clip's pitch never changes, and measuring one costs ~80 ms. Re-measuring the
# whole pool on every re-roll made the button take seconds; cache it on disk so
# the pool can grow without the button getting slower.
PITCH_CACHE = ".pitches.json"


def _cache_file(directory: Path) -> Path:
    return directory / PITCH_CACHE


def _load_pitch_cache(directory: Path) -> dict[str, Any]:
    try:
        return json.loads(_cache_file(directory).read_text())
    except (OSError, ValueError):
        return {}


def _save_pitch_cache(directory: Path, cache: dict[str, Any]) -> None:
    path = _cache_file(directory)
    try:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(cache))
        temp.replace(path)          # atomic, so a reader never sees half a file
    except OSError:
        pass


def pitch_of(path: str | Path) -> float | None:
    """Cached pitch, keyed by the file's size and modification time."""
    path = Path(path)
    if not path.is_file():
        return None
    stat = path.stat()
    key = path.name
    cache = _load_pitch_cache(path.parent)
    entry = cache.get(key)
    if (
        isinstance(entry, dict)
        and entry.get("size") == stat.st_size
        and int(entry.get("mtime", -1)) == int(stat.st_mtime)
    ):
        return entry.get("hz")

    hz = estimate_pitch(path)
    cache[key] = {"size": stat.st_size, "mtime": int(stat.st_mtime), "hz": hz}
    _save_pitch_cache(path.parent, cache)
    return hz


def choose_distinct(candidates: list[tuple[Path, float | None]], count: int) -> list[Path]:
    """Pick voices that sound different from each other, by pitch spread."""
    measured = [(p, hz) for p, hz in candidates if hz]
    if not measured:
        return [p for p, _ in candidates[:count]]
    measured.sort(key=lambda item: item[1])
    if count == 1 or len(measured) <= count:
        return [p for p, _ in measured[:count]]

    # The narrator comes from the middle of the range; the others from the ends.
    middle = measured[len(measured) // 2]
    picked = [middle]
    remaining = [item for item in measured if item is not middle]
    while len(picked) < count and remaining:
        far = max(remaining, key=lambda item: min(abs(item[1] - p[1]) for p in picked))
        picked.append(far)
        remaining.remove(far)
    return [p for p, _ in picked]


def render_reference(backend: Any, text: str, out: Path) -> Path:
    """One unconditioned generation: whatever speaker the model happens to pick."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(backend.synth(text, voice="default"))
    out.with_suffix(".txt").write_text(text)   # its transcript travels with it
    return out


def silence_reference(out: Path, seconds: float = 1.0, rate: int = 24000) -> Path:
    """A placeholder so tests and dry runs do not need a speech server."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        step = 2 * math.pi * 140 / rate
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(step * i)))
                for i in range(int(seconds * rate))
            )
        )
    return out
