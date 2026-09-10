"""The Chrome extension ships in this repo, so its manifest is worth checking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "extension"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text())


def test_manifest_is_v3_and_complete(manifest):
    assert manifest["manifest_version"] == 3
    assert manifest["name"] == "readcast"
    assert manifest["background"]["service_worker"] == "background.js"
    assert manifest["options_page"] == "options.html"


def test_every_referenced_file_exists(manifest):
    referenced = [
        manifest["background"]["service_worker"],
        manifest["options_page"],
        *manifest["icons"].values(),
    ]
    for name in referenced:
        assert (EXT / name).is_file(), f"{name} is referenced but missing"
    assert (EXT / "options.js").is_file()


def test_permissions_stay_least_privilege(manifest):
    """activeTab covers click-triggered injection; a blanket host permission
    only buys a scarier install prompt."""
    assert "activeTab" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]
    assert "storage" in manifest["permissions"]
    for host in manifest["host_permissions"]:
        assert "127.0.0.1" in host or "localhost" in host, host
    assert "<all_urls>" not in manifest["host_permissions"]
    assert "https://*/*" not in manifest["host_permissions"]


def test_background_posts_the_page_html_with_a_bearer_token():
    js = (EXT / "background.js").read_text()
    assert "/jobs" in js
    assert "Bearer " in js
    assert "client_html" in js
    assert "document.documentElement.outerHTML" in js
    assert "4e6" in js  # the same 4 MB cap the server enforces
    # Failure modes the user will actually hit are handled by name.
    for status in ("401", "413"):
        assert status in js


def test_options_page_stores_host_and_token():
    js = (EXT / "options.js").read_text()
    assert "chrome.storage.sync.set" in js
    assert "host" in js and "token" in js
    html = (EXT / "options.html").read_text()
    assert 'id="host"' in html and 'id="token"' in html
