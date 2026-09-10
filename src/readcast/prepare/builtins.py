"""Step 4: the builtins. Numbers, currency, dates, ranges, percent, units,
URLs, symbols.

These carry the load. Order inside this step matters: dates before ranges, so
`2026-08-25` is not read as a range; currency before numbers, so `$1.2M` is one
frozen phrase rather than a digit run and a stray `M`.
"""

from __future__ import annotations

import re
from typing import Any

from num2words import num2words

from readcast.prepare.document import Doc

FILE = "normalize.yml"

CURRENCY_WORDS = {
    "$": ("dollars", "dollar", "cents", "cent"),
    "£": ("pounds", "pound", "pence", "penny"),
    "€": ("euros", "euro", "cents", "cent"),
    "¥": ("yen", "yen", "sen", "sen"),
}

MONTHS = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "may": "May", "jun": "June", "jul": "July", "aug": "August",
    "sep": "September", "sept": "September", "oct": "October",
    "nov": "November", "dec": "December",
}


def say_number(value: float | int) -> str:
    """num2words in a narration voice: no serial commas, no British `and`."""
    words = num2words(value)
    return words.replace(",", "").replace(" and ", " ")


def say_year(value: int) -> str:
    return num2words(value, to="year").replace(",", "").replace(" and ", " ")


def say_ordinal(value: int) -> str:
    return num2words(value, to="ordinal").replace(",", "").replace(" and ", " ")


def _to_number(raw: str) -> float | int:
    clean = raw.replace(",", "")
    return float(clean) if "." in clean else int(clean)


def _alternation(keys) -> str:
    return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))


# A comma inside a number is a thousands separator; a comma after one is
# punctuation. `2025, and` must not be read as the number `2025,`.
NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


# -- individual builtins ----------------------------------------------------

URL_RE = re.compile(
    r"(?:https?://|(?<![\w.])www\.)[^\s<>()\[\]{}\"']+"
    r"|(?<![\w.@])[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|ai|gov|edu|co\.uk)"
    r"(?:/[^\s<>()\[\]{}\"']*)"
)


def apply_urls(doc: Doc, cfg: dict[str, Any]) -> None:
    action = cfg.get("action", "drop")
    if action == "read":
        return
    replacement = cfg.get("drop_replacement", "")

    def repl(m: re.Match[str]) -> str | None:
        raw = m.group(0).rstrip(".,;:!?")
        if action == "say_domain":
            host = re.sub(r"^https?://", "", raw).split("/")[0]
            host = host[4:] if host.startswith("www.") else host
            return " " + host.replace(".", " dot ") + " "
        return replacement

    doc.apply(URL_RE, repl, rule="builtin:urls", file=FILE)


ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
NAMED_DATE_RE = re.compile(
    r"\b(" + _alternation(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(\d{4}))?\b",
    re.I,
)


def apply_dates(doc: Doc, cfg: dict[str, Any]) -> None:
    style = cfg.get("style", "month_day_year")
    month_names = list(MONTHS.values())

    def iso(m: re.Match[str]) -> str | None:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        name = month_names[month - 1]
        if style == "day_month_year":
            return f"the {say_ordinal(day)} of {name}, {say_year(year)}"
        return f"{name} {say_ordinal(day)}, {say_year(year)}"

    def named(m: re.Match[str]) -> str | None:
        month = MONTHS[m.group(1)[:4].lower().rstrip(".")] if m.group(1)[:4].lower() in MONTHS \
            else MONTHS.get(m.group(1)[:3].lower())
        if not month:
            return None
        day = int(m.group(2))
        if not 1 <= day <= 31:
            return None
        out = f"{month} {say_ordinal(day)}"
        if m.group(3):
            out += f", {say_year(int(m.group(3)))}"
        return out

    doc.apply(ISO_DATE_RE, iso, rule="builtin:dates", file=FILE)
    doc.apply(NAMED_DATE_RE, named, rule="builtin:dates", file=FILE)


def apply_currency(doc: Doc, cfg: dict[str, Any]) -> None:
    magnitudes = {k.upper(): v for k, v in (cfg.get("magnitudes") or {}).items()}
    mag_alt = _alternation(magnitudes) if magnitudes else "(?!)"
    pattern = re.compile(
        # The whitespace belongs to the magnitude, not to the number: without
        # the inner group, "$2.50 (12x" swallows the space before the paren.
        r"([$£€¥])\s?(" + NUM + r")(?:\s*(" + mag_alt + r"))?(?!\w)",
        re.I,
    )

    def repl(m: re.Match[str]) -> str | None:
        symbol, raw, mag = m.group(1), m.group(2), m.group(3)
        plural, singular, cents_plural, cents_singular = CURRENCY_WORDS[symbol]
        value = _to_number(raw)
        if mag:
            word = magnitudes[mag.upper()]
            return f"{say_number(value)} {word} {plural}"
        if isinstance(value, float):
            whole, _, frac = raw.replace(",", "").partition(".")
            if len(frac) == 2:
                dollars = int(whole)
                cents = int(frac)
                unit = singular if dollars == 1 else plural
                out = f"{say_number(dollars)} {unit}"
                if cents:
                    cent_unit = cents_singular if cents == 1 else cents_plural
                    out += f" and {say_number(cents)} {cent_unit}"
                return out
            return f"{say_number(value)} {plural}"
        return f"{say_number(value)} {singular if value == 1 else plural}"

    doc.apply(pattern, repl, rule="builtin:currency", file=FILE)


RANGE_RE = re.compile(
    r"(?<![\w-])(" + NUM + r")\s*[-–—]\s*(" + NUM + r")(?![\w-])"
)


def apply_ranges(doc: Doc, cfg: dict[str, Any]) -> None:
    joiner = cfg.get("joiner", " to ")

    def repl(m: re.Match[str]) -> str:
        return f"{say_number(_to_number(m.group(1)))}{joiner}{say_number(_to_number(m.group(2)))}"

    doc.apply(RANGE_RE, repl, rule="builtin:ranges", file=FILE)


PERCENT_RE = re.compile(r"(" + NUM + r")\s*%")
BARE_PERCENT_RE = re.compile(r"\s*%")


def apply_percent(doc: Doc, cfg: dict[str, Any]) -> None:
    doc.apply(
        PERCENT_RE,
        lambda m: f"{say_number(_to_number(m.group(1)))} percent",
        rule="builtin:percent",
        file=FILE,
    )
    # Nothing may reach the engine as a bare symbol.
    doc.apply(BARE_PERCENT_RE, lambda m: " percent", rule="builtin:percent", file=FILE)


def apply_units(doc: Doc, cfg: dict[str, Any]) -> None:
    table = cfg.get("table") or {}
    if not table:
        return
    pattern = re.compile(
        r"(?<![\w])(" + NUM + r")\s*(" + _alternation(table) + r")(?![\w])"
    )
    exact = {k: v for k, v in table.items()}

    def repl(m: re.Match[str]) -> str | None:
        raw, unit = m.group(1), m.group(2)
        word = exact.get(unit)
        if word is None:  # case-insensitive fallback, e.g. Kg for kg
            for key, value in exact.items():
                if key.lower() == unit.lower():
                    word = value
                    break
        if word is None:
            return None
        value = _to_number(raw)
        if value == 1 and word.endswith("s"):
            word = word[:-1]
        return f"{say_number(value)} {word}"

    doc.apply(pattern, repl, rule="builtin:units", file=FILE)

    # Symbol units survive when their number was consumed by an earlier rule.
    symbolic = [k for k in table if not k.isalnum()]
    if symbolic:
        bare = re.compile(r"\s*(" + _alternation(symbolic) + r")(?!\w)")
        doc.apply(
            bare,
            lambda m: " " + str(exact[m.group(1)]),
            rule="builtin:units",
            file=FILE,
        )


ORDINAL_RE = re.compile(r"(?<![\w])(\d{1,3}(?:,\d{3})*)(st|nd|rd|th)(?![\w])", re.I)


def apply_ordinals(doc: Doc) -> None:
    doc.apply(
        ORDINAL_RE,
        lambda m: say_ordinal(int(m.group(1).replace(",", ""))),
        rule="builtin:ordinals",
        file=FILE,
    )


# A digit run next to a letter belongs to an identifier (H100, GPT-5). Leave it
# for the lexicon; spelling it here produces "H one hundred".
# Also guard the tail of a version string: "v1.1" was read as "v1.one".
NUMBER_RE = re.compile(r"(?<!\w)(?<!\w-)(?<!\w\.)(" + NUM + r")(?!\w)")

# "the 1990s" is a decade, not the number 199 followed by "0s".
DECADE_RE = re.compile(r"(?<!\w)(\d{4})s(?!\w)")


def _pluralize_year(words: str) -> str:
    head, _, last = words.rpartition(" ")
    plural = last[:-1] + "ies" if last.endswith("y") else last + "s"
    return f"{head} {plural}".strip()


def apply_decades(doc: Doc, cfg: dict[str, Any]) -> None:
    def repl(m: re.Match[str]) -> str | None:
        year = int(m.group(1))
        if not 1100 <= year <= 2099:
            return None
        return _pluralize_year(say_year(year))

    doc.apply(DECADE_RE, repl, rule="builtin:decades", file=FILE)


def apply_numbers(doc: Doc, cfg: dict[str, Any]) -> None:
    max_digits = int(cfg.get("max_digits_spelled", 9))
    year_style = cfg.get("year_style", "pairs")

    def repl(m: re.Match[str]) -> str | None:
        raw = m.group(1)
        digits = re.sub(r"[^\d]", "", raw)
        if len(digits) > max_digits:
            return None  # a long run is an identifier, not a quantity
        value = _to_number(raw)
        if (
            year_style == "pairs"
            and isinstance(value, int)
            and "," not in raw
            and len(digits) == 4
            and 1100 <= value <= 2099
        ):
            return say_year(value)
        return say_number(value)

    doc.apply(NUMBER_RE, repl, rule="builtin:numbers", file=FILE)


def apply_symbols(doc: Doc, cfg: dict[str, Any]) -> None:
    table = cfg.get("table") or {}
    if not table:
        return
    pattern = re.compile("(" + _alternation(table) + ")")

    def repl(m: re.Match[str]) -> str | None:
        symbol = m.group(1)
        if symbol == ">":
            # A line-leading "> " is a blockquote marker, not a comparison.
            line_start = doc.text.rfind("\n", 0, m.start()) + 1
            if doc.text[line_start : m.start()].strip() == "":
                return None
        return f" {table[symbol]} "

    doc.apply(pattern, repl, rule="builtin:symbols", file=FILE)


ORDER = ("urls", "dates", "currency", "ranges", "percent", "units", "numbers", "symbols")


def apply_builtins(doc: Doc, builtins: dict[str, Any]) -> None:
    def enabled(name: str) -> dict[str, Any] | None:
        cfg = builtins.get(name) or {}
        return cfg if cfg.get("enabled", True) else None

    for name in ORDER:
        cfg = enabled(name)
        if cfg is None:
            continue
        if name == "urls":
            apply_urls(doc, cfg)
        elif name == "dates":
            apply_dates(doc, cfg)
        elif name == "currency":
            apply_currency(doc, cfg)
        elif name == "ranges":
            apply_ranges(doc, cfg)
        elif name == "percent":
            apply_percent(doc, cfg)
        elif name == "units":
            apply_units(doc, cfg)
        elif name == "numbers":
            if cfg.get("ordinals", True):
                apply_ordinals(doc)
            apply_decades(doc, cfg)
            apply_numbers(doc, cfg)
        elif name == "symbols":
            apply_symbols(doc, cfg)
        doc.collapse_whitespace()
