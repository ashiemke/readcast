"""Configuration loading.

One YAML file. Every stage reads from the object this module returns, so a
tuning knob is always one edit away from taking effect.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAME = "config.yml"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


DEFAULTS: dict[str, Any] = {
    "base_url": "http://127.0.0.1:8000",
    "api_token": "CHANGE_ME",
    "data_dir": "./data",
    "rules_dir": "./rules",
    "tts": {
        "backend": "mlx",
        "voice": "default",
        "mlx_url": "http://127.0.0.1:8080/v1",
        "model": "mlx-community/Breeze-TTS-2-mlx",
        "instructions": {
            "intro": "Read as a brief announcement. Neutral and clear.",
            "heading": "Read as a section heading. Slightly slower, with a falling tone.",
            "body": "Read as narration for an audiobook. Calm, even pace.",
            "quote": "Read as a quotation from another writer. Slightly softer.",
            "aside": "Read as an aside. Lighter and a little quicker.",
        },
        # per_episode: each article gets a narrator from the pool, stable
        # throughout that article. fixed: the same three voices every time.
        "voice_mode": "per_episode",
        "voice_pool": "./data/voices/pool",
        "voices": {
            "main": "./data/voices/main.wav",
            "quote": "./data/voices/quote.wav",
            "aside": "./data/voices/aside.wav",
        },
        "roles": {
            "intro": "main", "heading": "main", "body": "main",
            "quote": "quote", "aside": "aside",
        },
        "backends": {},
    },
    "pipeline": {
        # true: prepared text waits for `readcast release <id>` before audio.
        "hold_for_review": False,
    },
    "chunk": {"target_chars": 300, "max_chars": 450},
    "verify": {
        "enabled": True,
        "max_cer": 0.15,
        "retries": 2,
        "min_chars": 40,
        "whisper_model": "mlx-community/whisper-small-mlx",
    },
    "audio": {
        "lufs": -16,
        "true_peak": -1.5,
        "lra": 11,
        "bitrate_kbps": 64,
        "sample_rate": 24000,
        "pause_paragraph_ms": 500,
        "pause_heading_ms": 1200,
        "pause_after_intro_ms": 1000,
    },
    "fetch": {
        "timeout_s": 20,
        "max_client_html_bytes": 4 * 1024 * 1024,
        "min_extracted_chars": 500,
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    },
    "feed": {
        "title": "readcast",
        "author": "",
        "description": "Articles, read aloud.",
        "language": "en-us",
        "max_items": 100,
        "intro_template": "{title}. From {publication}. By {author}. Published {published_at}.",
    },
}


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def root(self) -> Path:
        """Directory the config file lives in. Relative paths resolve from here."""
        return self.path.parent if self.path else Path.cwd()

    def _resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def data_dir(self) -> Path:
        return self._resolve(self.raw["data_dir"])

    @property
    def rules_dir(self) -> Path:
        return self._resolve(self.raw["rules_dir"])

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def feed_dir(self) -> Path:
        return self.data_dir / "feed"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "readcast.db"

    @property
    def base_url(self) -> str:
        return str(self.raw["base_url"]).rstrip("/")

    @property
    def api_token(self) -> str:
        return str(self.raw["api_token"])

    def backend_settings(self, name: str) -> dict[str, Any]:
        """Settings for one TTS backend, with the top-level tts keys as the floor."""
        tts = self.raw["tts"]
        common = {
            "voice": tts.get("voice", "default"),
            "instructions": tts.get("instructions", {}),
        }
        if name == "mlx":
            common |= {"url": tts.get("mlx_url"), "model": tts.get("model")}
        per = (tts.get("backends") or {}).get(name) or {}
        return _deep_merge(common, per)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.jobs_dir, self.feed_dir):
            d.mkdir(parents=True, exist_ok=True)


def hold_new_jobs(cfg: "Config", db: Any) -> bool:
    """Should a freshly prepared job wait for the operator?

    The stored setting wins so the switch can be thrown from the dashboard
    without editing a file; config.yml supplies the starting position.
    """
    stored = db.get_setting("hold_for_review") if db is not None else None
    if stored is not None:
        return str(stored) == "1"
    return bool((cfg.get("pipeline") or {}).get("hold_for_review"))


def set_hold_new_jobs(db: Any, value: bool) -> bool:
    db.set_setting("hold_for_review", "1" if value else "0")
    return value


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Locate config.yml: an explicit path, then $READCAST_CONFIG, then upward from cwd."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("READCAST_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        p = candidate / DEFAULT_CONFIG_NAME
        if p.is_file():
            return p
    return here / DEFAULT_CONFIG_NAME


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg_path = find_config(path)
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    # Deep copy: a nested key absent from the file would otherwise alias the
    # module-level DEFAULTS, and mutating one Config would corrupt them all.
    merged = _deep_merge(copy.deepcopy(DEFAULTS), raw)
    if os.environ.get("READCAST_DATA_DIR"):
        merged["data_dir"] = os.environ["READCAST_DATA_DIR"]
    if os.environ.get("READCAST_RULES_DIR"):
        merged["rules_dir"] = os.environ["READCAST_RULES_DIR"]
    if os.environ.get("READCAST_API_TOKEN"):
        merged["api_token"] = os.environ["READCAST_API_TOKEN"]
    return Config(raw=merged, path=cfg_path)
