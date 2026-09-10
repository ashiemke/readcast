"""Golden files. These catch extraction regressions, which rule tests cannot see.

Acceptance test 2: spoken.txt for the fixture articles contains no `[`, no
`Fig.`, no `$`, no `%`, and no `http`.
"""

from __future__ import annotations

import difflib

import pytest

from readcast.extract import extract
from readcast.prepare.loader import load_rules
from readcast.prepare.pipeline import prepare_text
from tests.conftest import REPO, fixture_html, golden

CASES = {
    "tech_review": "https://arstechnica.com/gadgets/datacenter-bill",
    "substack_post": "https://example.substack.com/p/shipping-slowly",
    "research_brief": "https://asr.example.org/thermal-drift",
}


def run_fixture(name: str, url: str):
    ruleset = load_rules(REPO / "rules", url)
    extracted = extract(fixture_html(name), url, ruleset.extract)
    return extracted, prepare_text(extracted.markdown, ruleset)


@pytest.mark.parametrize("name,url", CASES.items())
def test_spoken_matches_the_golden_file(name, url):
    _, result = run_fixture(name, url)
    expected = golden(name)
    if result.spoken != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(), result.spoken.splitlines(),
                fromfile="golden", tofile="actual", lineterm="",
            )
        )
        pytest.fail(f"{name} drifted from its golden file:\n{diff}")


@pytest.mark.parametrize("name,url", CASES.items())
def test_no_forbidden_text_reaches_the_engine(name, url):
    _, result = run_fixture(name, url)
    for forbidden in ("[", "Fig.", "$", "%", "http"):
        assert forbidden not in result.spoken, f"{forbidden!r} survived in {name}"


@pytest.mark.parametrize("name,url", CASES.items())
def test_structure_survives_extraction(name, url):
    extracted, result = run_fixture(name, url)
    assert len(extracted) > 500
    assert extracted.title
    assert result.spoken.startswith("## ")
    assert "\n\n" in result.spoken  # paragraph breaks are preserved


def test_metadata_falls_back_through_og_and_json_ld():
    extracted = extract(fixture_html("substack_post"), CASES["substack_post"])
    assert extracted.title == "Notes on shipping slowly"
    assert extracted.author == "Marta Ellis"
    assert extracted.publication == "The Slow Build"
    assert str(extracted.published_at).startswith("2026-07-14")


def test_code_and_tables_do_not_reach_the_engine():
    _, result = run_fixture("tech_review", CASES["tech_review"])
    assert "SELECT" not in result.spoken
    assert "42 MW" not in result.spoken


def test_domain_drop_selectors_remove_the_promo():
    _, result = run_fixture("tech_review", CASES["tech_review"])
    assert "Subscribe to" not in result.spoken
    assert "covers infrastructure" not in result.spoken


def test_unknowns_are_written_with_counts_and_examples(tmp_path):
    _, result = run_fixture("tech_review", CASES["tech_review"])
    terms = {u["term"]: u for u in result.unknowns}
    assert "H100" in terms
    assert terms["H100"]["count"] >= 1
    assert terms["H100"]["example"]
    assert terms["H100"]["suggested"] in ("spell", "respell")
    # A term with a lexicon entry is not an unknown.
    assert "AWS" not in terms
    result.write(tmp_path)
    assert (tmp_path / "spoken.txt").is_file()
    assert (tmp_path / "transforms.jsonl").read_text().strip()
    assert (tmp_path / "unknowns.jsonl").read_text().strip()


def test_emphasis_is_stripped_including_nested_and_multiline():
    """One stray asterisk used to pair with another paragraphs away."""
    ruleset = load_rules(REPO / "rules")

    nested = prepare_text(
        "**We kept an old Servant whose name was *Wright*, in constant Work.**",
        ruleset, collect_unknowns=False,
    ).spoken
    assert "*" not in nested
    assert "Wright" in nested and "Servant" in nested

    across_lines = prepare_text(
        "*He said\nGood-Morrow Father Wright*; the old Fellow looks up.",
        ruleset, collect_unknowns=False,
    ).spoken
    assert "*" not in across_lines
    assert "Good-Morrow" in across_lines

    # An unpaired asterisk must not swallow the paragraph after it.
    unpaired = prepare_text(
        "A paragraph with one *stray asterisk.\n\nA second paragraph survives.",
        ruleset, collect_unknowns=False,
    ).spoken
    assert "*" not in unpaired
    assert "A second paragraph survives." in unpaired
    assert "stray asterisk" in unpaired


def test_a_pdf_is_recognised_rather_than_reported_as_empty():
    """Chrome shows a PDF in its own viewer, so the captured page is a shell."""
    from readcast.extract import looks_like_pdf

    chrome_shell = (
        '<html><head><link rel="stylesheet" '
        'href="chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/pdf_embedder.css">'
        "</head><body></body></html>"
    )
    assert looks_like_pdf(chrome_shell, "https://arxiv.org/pdf/2603.18161")
    assert looks_like_pdf("<html></html>", "https://example.org/paper.pdf")
    assert looks_like_pdf("%PDF-1.7\nbinary junk here")
    assert looks_like_pdf(chrome_shell)

    # A real article is not mistaken for one.
    assert not looks_like_pdf(fixture_html("tech_review"), CASES["tech_review"])
    assert not looks_like_pdf("<html><body>" + "word " * 2000 + "</body></html>")
