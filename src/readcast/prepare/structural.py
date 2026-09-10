"""Step 1: structural strip.

Removes whole blocks from the extracted markdown before any span-level rule
runs. Nothing here reaches the speech engine.

Headings keep a `## ` prefix in spoken.txt. The operator wants to see section
boundaries in the file; the chunker strips the marker before synthesis.
"""

from __future__ import annotations

import re

FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?(?:\n(?:.*?\n)*?[ \t]*\1[ \t]*)?$", re.M)
TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.M)
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*(?:\([^)]*\))?[^)]*)\)")
HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", re.M)
SETEXT = re.compile(r"^[ \t]{0,3}(\S.*)\n[ \t]{0,3}(=+|-{2,})[ \t]*$", re.M)
QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
RULE_LINE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$", re.M)
LIST_MARKER = re.compile(
    r"^[ \t]*(?:[-*+\u2022\u2023\u25aa\u25cf\u25e6\u00b7\u2043][ \t]*"
    r"|\d{1,3}[.)][ \t]+)",
    re.M,
)
# Bullets also turn up mid-line in extracted markup, where they are furniture.
BULLET = re.compile(r"[\u2022\u2023\u25aa\u25cf\u25e6\u2043]")
# Emphasis can wrap several lines of a blockquote, so newlines are allowed,
# but it never crosses a paragraph break — without that bound one stray
# asterisk pairs with another thousands of characters away.
EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})((?:(?!\n\s*\n).)*?\S)\1", re.S)
STRAY_ASTERISK = re.compile(r"\*+")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
# trafilatura renders a <pre><code> block as one backticked line, not a fence.
CODE_LINE = re.compile(r"^[ \t]*`([^`\n]+)`[ \t]*$", re.M)
# trafilatura leaves some inline markup in place (Wikipedia's <sup> citation
# markers, for one). A tag read aloud is worse than no tag at all.
HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:.*$", re.M)
FOOTNOTE_REF = re.compile(r"\[\^[^\]]+\]")


def _log(sink: list, rule: str, offset: int, before: str, after: str) -> None:
    if before == after:
        return
    sink.append(
        {
            "rule": rule,
            "file": "strip.yml",
            "offset": offset,
            "before": before if len(before) <= 200 else before[:197] + "...",
            "after": after,
        }
    )


def structural_strip(text: str, structural: dict, transforms: list) -> str:
    """Apply the block-level policy in strip.yml. Returns markdown-lite text."""
    code_policy = structural.get("code_blocks", "drop")
    announce = structural.get("code_announce_text", "Code block omitted.")

    def on_fence(m: re.Match[str]) -> str:
        body = m.group(0)
        out = f"\n{announce}\n" if code_policy == "announce" else "\n"
        _log(transforms, "structural:code_blocks", m.start(), body, out.strip())
        return out

    text = FENCE.sub(on_fence, text)
    text = CODE_LINE.sub(on_fence, text)

    def on_tag(m: re.Match[str]) -> str:
        _log(transforms, "structural:html_tag", m.start(), m.group(0), "")
        return ""

    text = HTML_TAG.sub(on_tag, text)

    if structural.get("tables", "drop") == "drop":

        def on_table(m: re.Match[str]) -> str:
            _log(transforms, "structural:tables", m.start(), m.group(0), "")
            return ""

        text = TABLE_ROW.sub(on_table, text)

    if structural.get("image_alt", "drop") == "drop":

        def on_image(m: re.Match[str]) -> str:
            _log(transforms, "structural:image_alt", m.start(), m.group(0), "")
            return ""

        text = IMAGE.sub(on_image, text)

    # A link reads as its text. The target is noise in audio; the URL builtin
    # would only have to delete it later.
    def on_link(m: re.Match[str]) -> str:
        _log(transforms, "structural:link", m.start(), m.group(0), m.group(1))
        return m.group(1)

    text = LINK.sub(on_link, text)
    text = FOOTNOTE_DEF.sub("", text)
    text = FOOTNOTE_REF.sub("", text)

    if structural.get("headings", "keep") == "keep":
        text = SETEXT.sub(lambda m: f"## {m.group(1)}", text)
        text = HEADING.sub(lambda m: f"## {m.group(2)}", text)
    else:
        text = SETEXT.sub("", text)
        text = HEADING.sub("", text)

    if structural.get("blockquotes", "keep") == "keep":
        # Keep the marker: spoken.txt shows the operator where quotes are, and
        # the chunker uses it to give them their own voice. It is stripped
        # before synthesis, like the heading marker.
        text = QUOTE.sub("> ", text)
    else:
        text = re.sub(r"^[ \t]{0,3}>.*$", "", text, flags=re.M)

    text = RULE_LINE.sub("", text)
    text = LIST_MARKER.sub("", text)
    text = BULLET.sub(" ", text)
    text = INLINE_CODE.sub(r"\1", text)
    # Nested emphasis (**a *b* c**) needs more than one pass: re.sub does not
    # rescan its own replacement.
    for _ in range(5):
        unwrapped = EMPHASIS.sub(r"\2", text)
        if unwrapped == text:
            break
        text = unwrapped
    # Anything left is unpaired markdown residue. An asterisk read aloud is
    # worse than a missing one.
    text = STRAY_ASTERISK.sub("", text)
    return text
