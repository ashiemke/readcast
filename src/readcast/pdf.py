"""Reading a PDF.

A PDF is a description of marks on a page, not a document with structure, so
everything an HTML extractor gets for free has to be recovered: paragraphs,
headings, and which parts are the article rather than the furniture around it.

Four things ruin a narrated paper, all of them visible in any real one:

- a word broken across a line break reads as two words ("transduc" "tion"),
- page numbers and running heads are read aloud as if they were prose,
- the reference list is a quarter of the document and is unlistenable,
- an equation flattens into symbol soup.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("readcast.pdf")

BACK_MATTER = (
    "References", "Bibliography", "Works Cited", "Appendix", "Appendices",
    "Acknowledgments", "Acknowledgements", "Supplementary Material",
    "Supplementary Materials", "Disclosure", "Disclosures",
    "Conflicts of Interest", "Competing Interests", "Funding", "Author Contributions",
)

# "1 Introduction", "3.2 Attention", "A.1 Hyperparameters"
NUMBERED_HEADING = re.compile(
    r"^[ \t]*((?:\d+|[A-Z])(?:\.\d+)*)[.)]?[ \t]+([A-Z][^\n]{2,70}?)[ \t]*$", re.M
)
KNOWN_HEADING = re.compile(
    r"^[ \t]*(Abstract|Introduction|Background|Related Work|Method(?:s|ology)?|"
    r"Results|Discussion|Conclusions?|" + "|".join(BACK_MATTER) + r")[ \t]*$",
    re.M | re.I,
)
PAGE_NUMBER = re.compile(r"^[ \t]*(?:page[ \t]+)?\d{1,4}[ \t]*$", re.M | re.I)
# "word-\nnext" is one word the typesetter split; "well-\nknown" is two.
HYPHEN_BREAK = re.compile(r"(\w{2,})-\n([\w-]{2,})")
ARXIV_ID = re.compile(
    r"arXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?|\[[a-z]{2}\.[A-Z]{2}\]\s*\d+\s+\w+\s+\d{4}",
    re.I,
)
# A title may wrap, but it does not end on a preposition or a conjunction.
# "…Transformers for" continues; "Attention Is All You Need" does not.
DANGLING = re.compile(
    r"\b(for|of|and|or|the|a|an|in|on|with|to|from|by|as|at|via|using|towards?|"
    r"through|into|over|under|between|against|about)$|[-:,]$",
    re.I,
)
NOT_A_TITLE = re.compile(
    r"permission|copyright|licen[cs]e|all rights reserved|proceedings of|"
    r"conference on|preprint|under review|submitted to|attribution|arxiv|doi",
    re.I,
)
DOI = re.compile(r"\bdoi:\s*\S+", re.I)
# A line that is mostly symbols is an equation, not a sentence.
EQUATION_LINE = re.compile(r"^[^A-Za-z\n]*[=+×∑∏∫≈≤≥∈][^A-Za-z\n]*$", re.M)

# Typesetters emit these as single glyphs. Left alone they reach the engine as
# characters it has never seen, and they defeat every wordlist lookup.
LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u00a0": " ",
}


def unligature(text: str) -> str:
    for glyph, plain in LIGATURES.items():
        text = text.replace(glyph, plain)
    return text


def _title_from_page(page: str) -> str | None:
    """Read the title off the first page.

    A first page opens with a licence grant or a submission stamp as often as
    with the title, and a title that fills two lines is normal.
    """
    lines = [" ".join(l.split()) for l in unligature(page).splitlines()]
    for i, candidate in enumerate(lines):
        if not 8 <= len(candidate) <= 160:
            continue
        if NOT_A_TITLE.search(candidate) or candidate.endswith((",", ";")):
            continue
        # A lowercase opening is the middle of a wrapped sentence, which is
        # what a licence block looks like.
        if not candidate[:1].isupper():
            continue
        if sum(c.isdigit() for c in candidate) > len(candidate) / 4:
            continue
        # "BERT: Pre-training of … Transformers for" / "Language
        # Understanding": only join when the line cannot be the end of a title.
        if DANGLING.search(candidate) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if (
                4 <= len(nxt) <= 90
                and "@" not in nxt
                and len(candidate) + len(nxt) <= 200
                and not NOT_A_TITLE.search(nxt)
            ):
                return f"{candidate} {nxt}".strip()
        return candidate
    return None


@dataclass
class PdfDocument:
    text: str
    title: str | None = None
    author: str | None = None
    pages: int = 0


def _dehyphenate(text: str) -> str:
    """Rejoin words the typesetter broke, keeping real compounds intact.

    "transduc-/tion" is one word split across a line; "well-/known" and
    "English-/to-German" are hyphenated compounds. The bundled wordlist settles
    which is which far better than any rule about capitals.
    """
    from readcast.prepare.unknowns import english_words

    words = english_words()

    def join(m: re.Match[str]) -> str:
        left, right = m.group(1), m.group(2)
        if (left + right).lower() in words:
            return f"{left}{right}"                 # a split word
        if left.lower() in words and right.lower() in words:
            return f"{left}-{right}"                # two words, hyphenated
        if right[:1].isupper() or "-" in right:
            return f"{left}-{right}"                # "English-to-German"
        return f"{left}{right}"                     # most line breaks are splits

    return HYPHEN_BREAK.sub(join, text)


def _running_heads(pages: list[str], min_repeats: int | None = None) -> set[str]:
    """Lines repeated at the top or bottom of many pages are furniture."""
    if min_repeats is None:
        # Proportional: two repeats mean something in a four-page paper and
        # nothing in a forty-page one.
        min_repeats = max(2, len(pages) // 4)
    counts: dict[str, int] = {}
    for page in pages:
        lines = [l.strip() for l in page.splitlines() if l.strip()]
        # Count each line once per page: on a short page the top and bottom
        # slices overlap, and a body line would look like a running head.
        edges = {l for l in lines[:2] + lines[-2:] if 3 <= len(l) <= 90}
        for line in edges:
            counts[line] = counts.get(line, 0) + 1
    return {line for line, n in counts.items() if n >= min_repeats}


def _drop_back_matter(text: str, headings: tuple[str, ...]) -> tuple[str, str | None]:
    """Cut everything from the first back-matter heading to the end."""
    pattern = re.compile(
        r"^[ \t]*(?:\d+(?:\.\d+)*[.)]?[ \t]+)?(" + "|".join(
            re.escape(h) for h in headings
        ) + r")[ \t]*$",
        re.M | re.I,
    )
    match = pattern.search(text)
    if not match:
        return text, None
    # Only trust it in the last half; papers cite "Related Work" early on.
    if match.start() < len(text) * 0.4:
        later = pattern.search(text, int(len(text) * 0.4))
        if not later:
            return text, None
        match = later
    return text[: match.start()].rstrip(), match.group(1)


def to_markdown(pages: list[str], options: dict[str, Any] | None = None) -> str:
    """Turn extracted page text into the markdown-lite the rules stage expects."""
    options = options or {}
    furniture = _running_heads(pages) if options.get("drop_running_heads", True) else set()

    body: list[str] = []
    for page in pages:
        kept = [
            line for line in page.splitlines()
            if line.strip() not in furniture
        ]
        body.append("\n".join(kept))
    text = "\n".join(body)

    text = unligature(text)
    if options.get("dehyphenate", True):
        text = _dehyphenate(text)
    if options.get("drop_page_numbers", True):
        text = PAGE_NUMBER.sub("", text)
    if options.get("drop_identifiers", True):
        text = ARXIV_ID.sub("", text)
        text = DOI.sub("", text)
    if options.get("drop_equations", True):
        text = EQUATION_LINE.sub("", text)

    if options.get("drop_back_matter", True):
        headings = tuple(options.get("back_matter") or BACK_MATTER)
        text, cut = _drop_back_matter(text, headings)
        if cut:
            log.info("dropped everything from %r onward", cut)

    # Headings, so the chunker can pause and the episode gets chapters. Blank
    # lines around them keep the unwrapping below from absorbing the paragraph
    # that follows.
    text = KNOWN_HEADING.sub(lambda m: f"\n\n## {m.group(1).title()}\n\n", text)
    text = NUMBERED_HEADING.sub(lambda m: f"\n\n## {m.group(2)}\n\n", text)

    if options.get("drop_front_matter", True):
        # Everything before the abstract is licence grants, author lists,
        # affiliations and email addresses. None of it is the paper.
        first = re.search(r"^## (Abstract|Introduction)\b", text, re.M)
        if first is None:
            first = re.search(r"^## ", text, re.M)
        if first and first.start() > 0:
            dropped = len(text[: first.start()].split())
            text = text[first.start():]
            log.info("dropped %d words of front matter", dropped)


    # A single newline inside a paragraph is a line wrap, not a break.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_pdf(data: bytes, options: dict[str, Any] | None = None) -> PdfDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise RuntimeError(
            "reading PDFs needs pypdf; run `uv sync` to install it"
        ) from exc

    import io

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = to_markdown(pages, options)

    meta = reader.metadata or {}
    title = (meta.get("/Title") or "").strip() or None
    from_page = _title_from_page(pages[0]) if pages else None
    if title and pages and from_page:
        # Prefer the page when the metadata disagrees with it — a reused LaTeX
        # template can leave a title naming another paper. Only ever swap one
        # title for another, never discard the only one there is.
        opening = " ".join(unligature(pages[0]).lower().split())
        if " ".join(title.lower().split()[:4]) not in opening:
            log.info("the PDF's title %r is not on its first page; reading it instead",
                     title)
            title = from_page
    if not title or NOT_A_TITLE.search(title):
        title = from_page or title
    author = (meta.get("/Author") or "").strip() or None
    if not title or NOT_A_TITLE.search(title):
        # The title is the first substantial line that is not boilerplate: a
        # first page opens with licence grants and submission stamps as often
        # as it opens with the title.
        title = None
        for line in (pages[0].splitlines() if pages else []):
            candidate = " ".join(line.split())
            if not 8 <= len(candidate) <= 160:
                continue
            if NOT_A_TITLE.search(candidate) or candidate.endswith(("," , ";")):
                continue
            # A lowercase opening means this is the middle of a wrapped
            # sentence, which is what a licence block looks like.
            if not candidate[:1].isupper():
                continue
            if sum(c.isdigit() for c in candidate) > len(candidate) / 4:
                continue
            title = candidate
            break
    return PdfDocument(text=text, title=title, author=author, pages=len(reader.pages))
