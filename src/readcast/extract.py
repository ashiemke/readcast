"""Stage 2: extract.

Keeps heading level, paragraph boundaries, blockquote markers and code fences,
because the preparation stage and the chunker both read that structure.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import trafilatura
from urllib.parse import urlsplit

from lxml import html as lxml_html

log = logging.getLogger("readcast.extract")


@dataclass
class Extracted:
    markdown: str
    title: str | None = None
    author: str | None = None
    publication: str | None = None
    published_at: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.markdown or "")


def _selector_to_xpath(selector: str) -> str | None:
    """A deliberately small CSS subset: `tag`, `.class`, `#id`, and `tag.class`.

    Enough for the `drop_selectors` list in a domain file, with no extra
    dependency. Anything else is logged and skipped.
    """
    selector = selector.strip()
    m = re.fullmatch(r"([a-zA-Z][\w-]*)?(?:\.([\w-]+))?(?:#([\w-]+))?", selector)
    if not m or not any(m.groups()):
        return None
    tag, cls, ident = m.group(1) or "*", m.group(2), m.group(3)
    xpath = f"//{tag}"
    if cls:
        xpath += f"[contains(concat(' ', normalize-space(@class), ' '), ' {cls} ')]"
    if ident:
        xpath += f"[@id='{ident}']"
    return xpath


def drop_selectors(html: str, selectors: list[str]) -> str:
    if not selectors:
        return html
    try:
        tree = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001 - malformed markup is common
        return html
    removed = 0
    for selector in selectors:
        xpath = _selector_to_xpath(selector)
        if not xpath:
            log.warning("drop_selector %r is not supported; skipping", selector)
            continue
        for node in tree.xpath(xpath):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                removed += 1
    if removed:
        log.info("domain rules dropped %d element(s)", removed)
    return lxml_html.tostring(tree, encoding="unicode")


# Chrome renders a PDF in a built-in viewer, so a bookmarklet or extension
# captures an empty shell rather than the document.
PDF_VIEWER_MARKERS = ("pdf_embedder", "application/pdf", "<embed type=\"application/pdf")


def looks_like_pdf(html: str, url: str | None = None) -> bool:
    if url and urlsplit(url).path.lower().endswith(".pdf"):
        return True
    head = html[:2000].lower()
    if head.lstrip().startswith("%pdf"):
        return True
    return len(html) < 4000 and any(m in head for m in PDF_VIEWER_MARKERS)


def _og(tree: Any, prop: str) -> str | None:
    for xpath in (
        f"//meta[@property='{prop}']/@content",
        f"//meta[@name='{prop}']/@content",
    ):
        values = tree.xpath(xpath)
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return None


def _fallback_metadata(html: str) -> dict[str, str | None]:
    try:
        tree = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001
        return {}
    title = _og(tree, "og:title")
    if not title:
        node = tree.xpath("//title/text()")
        title = str(node[0]).strip() if node else None
    if not title:
        node = tree.xpath("//h1//text()")
        title = " ".join(str(t).strip() for t in node).strip() or None
    ld_author = ld_published = ld_publisher = None
    for raw in tree.xpath("//script[@type='application/ld+json']/text()"):
        try:
            data = json.loads(str(raw))
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            if isinstance(author, dict):
                ld_author = ld_author or author.get("name")
            elif isinstance(author, list) and author:
                first = author[0]
                ld_author = ld_author or (
                    first.get("name") if isinstance(first, dict) else str(first)
                )
            elif isinstance(author, str):
                ld_author = ld_author or author
            publisher = item.get("publisher")
            if isinstance(publisher, dict):
                ld_publisher = ld_publisher or publisher.get("name")
            ld_published = ld_published or item.get("datePublished")
    return {
        "title": title,
        "author": _og(tree, "article:author") or ld_author,
        "publication": _og(tree, "og:site_name") or ld_publisher,
        "published_at": _og(tree, "article:published_time") or ld_published,
    }


HEADING_LINE = re.compile(r"^#{1,6}\s+\S", re.M)


def _run_trafilatura(html: str, url: str | None, favor_precision: bool) -> str:
    return (
        trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=False,
            favor_precision=favor_precision,
            include_formatting=True,
            include_links=False,
            include_images=False,
        )
        or ""
    )


def extract(html: str, url: str | None = None, options: dict[str, Any] | None = None) -> Extracted:
    options = options or {}
    html = drop_selectors(html, list(options.get("drop_selectors") or []))
    favor_precision = bool(options.get("favor_precision", True))

    markdown = _run_trafilatura(html, url, favor_precision)

    # Precision mode drops every heading on some sites (Wikipedia, for one), and
    # the chunker, the pauses and the chapter marks all read headings. Fall back
    # only when it costs nothing: the recall pass must actually find some.
    if favor_precision and markdown and not HEADING_LINE.search(markdown):
        recall = _run_trafilatura(html, url, False)
        if HEADING_LINE.search(recall) and len(recall) >= len(markdown) * 0.9:
            log.info("precision mode found no headings; using the recall pass")
            markdown = recall

    title = author = publication = published = None
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
    except Exception:  # noqa: BLE001 - metadata parsing is best effort
        meta = None
    if meta is not None:
        title = meta.title or None
        author = meta.author or None
        publication = meta.sitename or None
        published = meta.date or None

    fallback = _fallback_metadata(html)
    title = title or fallback.get("title")
    author = author or fallback.get("author")
    publication = publication or fallback.get("publication")
    published = published or fallback.get("published_at")

    if author:
        author = re.sub(r"^\s*(by|By)\s+", "", str(author)).strip(" ;,")

    return Extracted(
        markdown=markdown.strip(),
        title=(title or None),
        author=(author or None),
        publication=(publication or None),
        published_at=(str(published) if published else None),
    )
