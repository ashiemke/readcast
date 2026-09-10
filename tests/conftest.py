"""Test rig: a temporary data dir, the repository's real rules, and a speech
backend that needs no model."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from readcast.config import load_config
from readcast.db import Database

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "rules", tmp_path / "rules")
    config = {
        "base_url": "https://readcast.test",
        "api_token": "test-token",
        "data_dir": "./data",
        "rules_dir": "./rules",
        "tts": {
            "backend": "test",
            "voice": "default",
            "backends": {"test": {"chars_per_second": 200}},
        },
        "chunk": {"target_chars": 300, "max_chars": 450},
        "verify": {"enabled": False},
        "audio": {"bitrate_kbps": 48, "pause_paragraph_ms": 300, "pause_heading_ms": 600},
        "feed": {
            "title": "readcast test",
            "author": "Test Operator",
            "max_items": 100,
            "intro_template": "{title}. From {publication}. By {author}.",
        },
    }
    (tmp_path / "config.yml").write_text(yaml.safe_dump(config))
    return tmp_path


@pytest.fixture
def cfg(workspace: Path):
    return load_config(workspace / "config.yml")


@pytest.fixture
def db(cfg) -> Database:
    cfg.ensure_dirs()
    return Database(cfg.db_path)


@pytest.fixture
def client(cfg, db):
    from fastapi.testclient import TestClient

    from readcast.api import create_app

    app = create_app(cfg, start_worker=False, db=db)
    with TestClient(app) as test_client:
        test_client.headers.update({"authorization": "Bearer test-token"})
        yield test_client


def fixture_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text()


def golden(name: str) -> str:
    return (FIXTURES / "golden" / f"{name}.spoken.txt").read_text()
