"""Step 7: unknown-term discovery.

The pipeline tells the operator what to tune. Catching every mispronunciation
by ear does not scale past a few episodes.
"""

from __future__ import annotations

import gzip
import re
from collections import Counter
from functools import lru_cache
from importlib.resources import files
from typing import Any, Iterable

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’./-]*[A-Za-z0-9]|[A-Za-z]")
SENTENCE_RE = re.compile(r"[^.!?\n]*[.!?]|[^.!?\n]+")
VOWELS = set("aeiouyAEIOUY")

REASONS = {
    "all_caps": "all uppercase, 2 to 6 characters",
    "internal_caps": "internal capitals",
    "alnum": "letters and digits mixed",
    "not_in_wordlist": "absent from the bundled wordlist and name list",
    "consonant_run": "three or more consonants in a row and no vowel",
}


@lru_cache(maxsize=1)
def english_words() -> frozenset[str]:
    data = files("readcast.data").joinpath("words_en.txt.gz").read_bytes()
    return frozenset(gzip.decompress(data).decode().split())


@lru_cache(maxsize=1)
def common_names() -> frozenset[str]:
    text = files("readcast.data").joinpath("names_common.txt").read_text()
    words = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        words.extend(w.lower() for w in line.split())
    return frozenset(words)


# The bundled wordlist is a dictionary, not a corpus: it holds "operator" but
# not "operators". Without this, every plural reads as an unknown term.
SUFFIX_RULES = (
    ("'s", ""), ("’s", ""), ("s", ""), ("es", ""), ("ies", "y"), ("ed", ""),
    ("ed", "e"), ("d", ""), ("ing", ""), ("ing", "e"), ("er", ""), ("er", "e"),
    ("est", ""), ("ly", ""), ("ness", ""), ("ment", ""), ("ings", ""),
)


def _known_word(word: str) -> bool:
    word = word.lower()
    if not word:
        return True
    words, names = english_words(), common_names()
    if word in words or word in names:
        return True
    for suffix, replacement in SUFFIX_RULES:
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            stem = word[: -len(suffix)] + replacement
            if stem in words or stem in names:
                return True
            # "shipping" -> "ship", "bigger" -> "big"
            if len(stem) > 2 and stem[-1] == stem[-2] and stem[:-1] in words:
                return True
    return False


def _known_token(token: str) -> bool:
    """A hyphenated token is known when every part of it is."""
    parts = [p for p in re.split(r"[-–—/.]", token) if p]
    if not parts:
        return True
    return all(_known_word(p) for p in parts)


def _classify(token: str) -> list[str]:
    bare = token.strip(".'’-")
    if not bare or len(bare) < 2:
        return []
    reasons: list[str] = []
    letters_only = bare.replace("-", "").replace(".", "").replace("'", "")
    if bare.isupper() and bare.isalpha() and 2 <= len(bare) <= 6:
        reasons.append("all_caps")
    if not bare.isupper() and any(c.isupper() for c in bare[1:]):
        reasons.append("internal_caps")
    if any(c.isdigit() for c in bare) and any(c.isalpha() for c in bare):
        reasons.append("alnum")
    if letters_only.isalpha():
        if not _known_token(bare):
            reasons.append("not_in_wordlist")
        run = 0
        for ch in letters_only:
            run = 0 if ch in VOWELS else run + 1
            if run >= 3 and not any(c in VOWELS for c in letters_only):
                reasons.append("consonant_run")
                break
    return reasons


def suggest_mode(token: str, reasons: Iterable[str]) -> str:
    reasons = set(reasons)
    bare = token.strip(".'’-")
    if "all_caps" in reasons and bare.isalpha():
        return "spell"
    return "respell"


def find_unknowns(text: str, known: set[str], max_terms: int = 200) -> list[dict[str, Any]]:
    """Return candidate terms with no lexicon entry, with a count and an example."""
    counts: Counter[str] = Counter()
    reasons_by_term: dict[str, list[str]] = {}
    example: dict[str, str] = {}

    for sentence in SENTENCE_RE.findall(text):
        stripped = sentence.strip()
        if not stripped or stripped.startswith("##"):
            continue
        for m in TOKEN_RE.finditer(stripped):
            token = m.group(0)
            bare = token.strip(".'’-")
            if not bare or bare.lower() in known:
                continue
            reasons = _classify(token)
            if not reasons:
                continue
            counts[bare] += 1
            reasons_by_term.setdefault(bare, reasons)
            example.setdefault(bare, " ".join(stripped.split())[:200])

    out = []
    for term, count in counts.most_common(max_terms):
        reasons = reasons_by_term[term]
        out.append(
            {
                "term": term,
                "count": count,
                "reasons": reasons,
                "why": ", ".join(REASONS[r] for r in reasons),
                "suggested": suggest_mode(term, reasons),
                "example": example[term],
            }
        )
    return out
