"""Step 6: the lexicon. Per-term pronunciation and expansion.

Terms are reserved before the builtins run and substituted after them. That
ordering is what lets `km/h` outrank the generic slash rule: the span is frozen
before any other rule can see it, and the replacement lands at the lexicon step.

Longest match first, so `AIX` never matches the rule for `AI`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from readcast.prepare.document import Doc

log = logging.getLogger("readcast.rules")
FILE = "lexicon.yml"

MODES = ("spell", "word", "respell", "replace", "ipa")


@dataclass
class Term:
    match: str
    mode: str
    say: str | None
    pattern: re.Pattern[str]
    note: str | None = None

    @property
    def key(self) -> str:
        return self.match.lower()


def _boundary(text: str, boundary: str) -> str:
    body = re.escape(text)
    if boundary != "word":
        return body
    lead = r"(?<!\w)" if text[:1].isalnum() or text[:1] == "_" else ""
    return f"{lead}{body}(?!\\w)"


def build_terms(defaults: dict[str, Any], entries: list[dict[str, Any]]) -> list[Term]:
    default_case = defaults.get("case", "sensitive")
    default_boundary = defaults.get("boundary", "word")
    terms: list[Term] = []
    for entry in entries:
        raw = entry.get("match")
        if not raw or entry.get("enabled", True) is False:
            continue
        mode = str(entry.get("mode", "respell"))
        if mode not in MODES:
            log.warning("lexicon term %r has unknown mode %r; treating as respell", raw, mode)
            mode = "respell"
        case = entry.get("case", default_case)
        boundary = entry.get("boundary", default_boundary)
        flags = re.I if case == "insensitive" else 0
        terms.append(
            Term(
                match=str(raw),
                mode=mode,
                say=entry.get("say"),
                pattern=re.compile(_boundary(str(raw), boundary), flags),
                note=entry.get("note"),
            )
        )
    # Longest first: the reservation pass claims `AIX` before `AI` is tried.
    terms.sort(key=lambda t: len(t.match), reverse=True)
    return terms


def render(term: Term, matched: str, supports_phonemes: bool = False) -> str:
    if term.mode == "spell":
        letters = [c for c in matched if not c.isspace()]
        return " ".join(letters)
    if term.mode == "word":
        return matched
    if term.mode == "ipa":
        if supports_phonemes and term.say:
            return str(term.say)
        log.warning(
            "lexicon term %r uses ipa but the backend takes no phonemes; "
            "falling back to respell",
            term.match,
        )
        return str(term.say) if term.say else matched
    return str(term.say) if term.say is not None else matched


def reserve_terms(doc: Doc, terms: list[Term]) -> None:
    """Claim every lexicon span before the builtins run."""
    for term in terms:
        for m in term.pattern.finditer(doc.text):
            if m.start() == m.end() or not doc.is_free(m.start(), m.end()):
                continue
            doc.reserve(m.start(), m.end(), term)


def apply_lexicon(doc: Doc, terms: list[Term], supports_phonemes: bool = False) -> None:
    """Substitute the reserved spans, then sweep for terms that appeared since."""
    for res in sorted(doc.reservations, key=lambda r: r.start, reverse=True):
        term = res.payload
        if not isinstance(term, Term):
            continue
        matched = doc.text[res.start : res.end]
        out = render(term, matched, supports_phonemes)
        doc.replace(
            res.start,
            res.end,
            out,
            rule=f"lexicon:{term.match}:{term.mode}",
            file=FILE,
            log=out != matched,
        )
    doc.reservations.clear()

    for term in terms:
        doc.apply(
            term.pattern,
            lambda m, t=term: render(t, m.group(0), supports_phonemes),
            rule=f"lexicon:{term.match}:{term.mode}",
            file=FILE,
        )
    doc.collapse_whitespace()
