"""Acceptance test 3: `rules test` exits zero."""

from __future__ import annotations

from pathlib import Path

from readcast.prepare.document import Doc
from readcast.prepare.loader import load_rules
from readcast.prepare.pipeline import prepare_text
from readcast.prepare.tests_runner import run_cases

REPO = Path(__file__).resolve().parent.parent


def _prepare(text: str, url: str | None = None) -> str:
    return prepare_text(text, load_rules(REPO / "rules", url), collect_unknowns=False).spoken.strip()


def test_every_case_in_tests_yml_passes():
    results = run_cases(REPO / "rules")
    failures = [r for r in results if not r.passed]
    assert not failures, "\n".join(f"{r.name}\n{r.diff()}" for r in failures)
    assert len(results) >= 7


def test_frozen_span_is_not_re_edited():
    import re

    doc = Doc("The budget was $3M.")
    doc.apply(re.compile(r"\$3M"), lambda m: "three million dollars",
              rule="currency", file="normalize.yml")
    doc.apply(re.compile(r"\bM\b|million"), lambda m: "MANGLED",
              rule="units", file="normalize.yml")
    assert doc.text == "The budget was three million dollars."


def test_longest_lexicon_match_wins():
    assert _prepare("The AIX box and the AI model.") == "The AIX box and the A I model."


def test_lexicon_outranks_a_generic_rule():
    assert _prepare("It ran at 90 km/h.") == "It ran at ninety kilometers per hour."


def test_transforms_record_which_rule_fired():
    result = prepare_text("Revenue hit $1.2M.", load_rules(REPO / "rules"), collect_unknowns=False)
    rules = {t["rule"] for t in result.transforms}
    assert "builtin:currency" in rules
    entry = next(t for t in result.transforms if t["rule"] == "builtin:currency")
    assert entry["before"] == "$1.2M"
    assert entry["after"] == "one point two million dollars"
    assert entry["file"] == "normalize.yml"


def test_domain_rules_merge_over_global():
    ruleset = load_rules(REPO / "rules", "https://arstechnica.com/some-post")
    matches = {t["match"]: t for t in ruleset.lexicon_terms}
    assert matches["Ars"]["say"] == "arse"
    assert any(p["id"] == "site-promo" for p in ruleset.strip_patterns)
    # The global rules are still there.
    assert any(p["id"] == "bracket-citation" for p in ruleset.strip_patterns)


def test_numbers_next_to_letters_are_left_alone():
    assert "H100" in _prepare("It trained on 512 H100 GPUs.")
    assert "five hundred twelve" in _prepare("It trained on 512 H100 GPUs.")


def test_decade_reads_as_a_decade():
    assert _prepare("A 1990s server room.") == "A nineteen nineties server room."


def test_thousands_separator_does_not_swallow_a_comma():
    assert _prepare("In 2025, it grew.") == "In twenty twenty-five, it grew."
    assert _prepare("It served 12,000 requests.") == "It served twelve thousand requests."


def test_currency_does_not_swallow_following_whitespace():
    assert _prepare("It is $2.50 (12x cheaper).") == (
        "It is two dollars and fifty cents (12x cheaper)."
    )
    assert _prepare("It cost $5 M total.") == "It cost five million dollars total."


def test_dollars_and_cents():
    assert _prepare("A fee of $1,234.56 applies.") == (
        "A fee of one thousand two hundred thirty-four dollars and fifty-six cents applies."
    )
    assert _prepare("It costs $1.") == "It costs one dollar."


def test_a_version_string_is_not_spelled_out():
    """"SQuAD v1.1" was read as "SQuAD v1.one"."""
    assert _prepare("Results on SQuAD v1.1 and v2.0.") == "Results on SQuAD v1.1 and v2.0."
    # A real decimal still reads as one.
    assert _prepare("It took 3.5 days.") == "It took three point five days."
    # A bare "2.1" is genuinely ambiguous, and "two point one" is how a person
    # reads a section number aloud anyway.
    assert _prepare("Section 2.1 explains.") == "Section two point one explains."
