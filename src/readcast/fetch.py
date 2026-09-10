"""Stage 1: fetch.

`client_html` is the important path. The browser already rendered the page and
already holds the operator's session, so a paywalled or JavaScript-heavy
article arrives intact. The network fetch is the fallback, not the other way
around.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("readcast.fetch")


class FetchError(RuntimeError):
    pass


def fetch_bytes(url: str, *, timeout_s: float = 20.0, user_agent: str) -> tuple[bytes, str]:
    """Fetch a URL as bytes, with its content type. PDFs are not text."""
    headers = {"user-agent": user_agent, "accept": "*/*"}
    try:
        with httpx.Client(
            follow_redirects=True, timeout=timeout_s, headers=headers
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content, response.headers.get("content-type", "")
    except httpx.HTTPError as exc:
        raise FetchError(f"fetch failed: {exc}") from exc


def is_pdf(data: bytes, content_type: str = "", url: str = "") -> bool:
    from urllib.parse import urlsplit

    if data[:5] == b"%PDF-":
        return True
    if "application/pdf" in content_type.lower():
        return True
    return urlsplit(url).path.lower().endswith(".pdf")


def fetch_url(url: str, *, timeout_s: float = 20.0, user_agent: str) -> str:
    headers = {
        "user-agent": user_agent,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
    }
    try:
        with httpx.Client(
            follow_redirects=True, timeout=timeout_s, headers=headers
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        raise FetchError(f"fetch failed: {exc}") from exc


def fetch_with_browser(url: str, *, timeout_s: float = 20.0, user_agent: str) -> str:
    """Second attempt: a real browser, waiting for the network to go idle."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise FetchError(
            "playwright is not installed; run `uv sync --extra browser` "
            "and `playwright install chromium`"
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
            except Exception:  # noqa: BLE001 - idle never arrives on some pages
                log.info("network never went idle for %s; taking the DOM as it stands", url)
            return page.content()
        finally:
            browser.close()
