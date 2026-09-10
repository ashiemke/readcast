"""Reading a PDF.

A PDF describes marks on a page, so everything an HTML extractor gets for free
has to be recovered — and the artifacts that ruin a narrated paper are all
present in any real one.
"""

from __future__ import annotations

import pytest

from readcast.pdf import read_pdf, to_markdown
from tests.pdfbuild import make_pdf

PAPER = [
    ["Provided proper attribution is provided, permission is granted to",
     "reproduce the tables in this paper.",
     "A Study Of Something Interesting",
     "Ada Lovelace", "Analytical Engine Co", "ada@example.org",
     "Abstract",
     "We examine a question at length, using methods that are transduc-",
     "tion based, and report a result of 38% over the baseline [13]."],
    ["1 Introduction",
     "The problem has been studied before [7, 12], though not well.",
     "Running head: A Study Of Something", "2"],
    ["2 Method",
     "We did the obvious thing, carefully, and then measured it.",
     "Running head: A Study Of Something", "3"],
    ["References",
     "[1] Jimmy Lei Ba. Layer normalization. arXiv:1607.06450, 2016.",
     "[2] Someone Else. Another paper entirely. 2017."],
]


@pytest.fixture(scope="module")
def paper():
    return read_pdf(make_pdf(PAPER, title="A Study Of Something Interesting"))


def test_a_word_broken_across_lines_is_rejoined(paper):
    """Otherwise "transduc" and "tion" are read as two words."""
    assert "transduction based" in paper.text
    assert "transduc- tion" not in paper.text
    assert "transduc-\ntion" not in paper.text


def test_a_real_compound_survives():
    doc = read_pdf(make_pdf([["Abstract", "It used an English-", "to-German corpus."]]))
    assert "English-to-German" in doc.text


def test_page_numbers_and_running_heads_are_dropped(paper):
    assert "Running head" not in paper.text
    for line in paper.text.splitlines():
        assert not line.strip().isdigit(), f"a page number was left in: {line!r}"


def test_the_reference_list_is_dropped(paper):
    assert "Layer normalization" not in paper.text
    assert "Another paper entirely" not in paper.text


def test_front_matter_is_dropped(paper):
    """Licence grants, authors, affiliations and emails are not the paper."""
    assert paper.text.startswith("## Abstract")
    assert "permission is granted" not in paper.text
    assert "ada@example.org" not in paper.text
    assert "Analytical Engine" not in paper.text


def test_sections_become_headings(paper):
    headings = [l for l in paper.text.splitlines() if l.startswith("## ")]
    assert "## Abstract" in headings
    assert "## Introduction" in headings
    assert "## Method" in headings
    # The section number is furniture; the name is the chapter title.
    assert not any(h.startswith("## 1 ") for h in headings)


def test_a_heading_does_not_swallow_its_paragraph(paper):
    for line in paper.text.splitlines():
        if line.startswith("## "):
            assert len(line) < 80, f"a paragraph was absorbed into a heading: {line[:90]!r}"


def test_wrapped_lines_become_paragraphs(paper):
    assert "The problem has been studied before" in paper.text
    body = [l for l in paper.text.splitlines() if l and not l.startswith("## ")]
    assert any(len(l) > 60 for l in body), "lines were never unwrapped into paragraphs"


def test_the_title_skips_licence_boilerplate(paper):
    assert paper.title == "A Study Of Something Interesting"


def test_a_title_is_found_when_the_metadata_has_none():
    doc = read_pdf(make_pdf([[
        "Provided proper attribution is provided, permission is granted to",
        "reproduce the tables in this paper.",
        "The Real Title Of The Paper",
        "Abstract", "Body text follows here."]]))
    assert doc.title == "The Real Title Of The Paper"


def test_the_cleanup_can_be_turned_off():
    pages = ["References\n[1] A citation.\n42"]   # page text, not lines
    kept = to_markdown(pages, {"drop_back_matter": False, "drop_page_numbers": False,
                               "drop_front_matter": False})
    assert "A citation" in kept
    assert "42" in kept


def test_a_pdf_with_no_text_layer_is_recognised():
    """A scan is images; there is nothing to read."""
    from readcast.worker import _extract_pdf, StageError
    import pathlib
    import tempfile

    empty = pathlib.Path(tempfile.mkdtemp()) / "scan.pdf"
    empty.write_bytes(make_pdf([[""]]))

    class Rules:
        extract: dict = {}

    with pytest.raises(StageError) as caught:
        _extract_pdf(empty, Rules(), min_chars=500)
    assert "scanned images" in str(caught.value)


def test_ligatures_are_normalised():
    """A typesetter's single glyph defeats every wordlist lookup, and the
    engine has never seen the character.

    Driven through to_markdown rather than the PDF fixture, which can only
    carry latin-1.
    """
    page = (
        "Abstract\n"
        "The model can be \ufb01netuned with task-\n"
        "speci\ufb01c modi\ufb01cations and su\ufb03cient data."
    )
    text = to_markdown([page])
    assert not any(g in text for g in "\ufb00\ufb01\ufb02\ufb03\ufb04")
    assert "finetuned" in text
    assert "sufficient" in text
    # With the ligature resolved, the wordlist can tell a compound from a split.
    assert "task-specific" in text


def test_smart_punctuation_is_flattened():
    text = to_markdown(["Abstract\nIt \u2019s a \u201cquoted\u201d word \u2013 and a dash."])
    assert "\u2019" not in text and "\u201c" not in text and "\u2013" not in text
    assert "quoted" in text


def test_metadata_that_disagrees_with_the_page_loses_to_the_page():
    data = make_pdf(
        [["The Actual Title Of This Paper", "Abstract", "Body text goes here."]],
        title="Some Entirely Different Paper",
    )
    assert read_pdf(data).title == "The Actual Title Of This Paper"


def test_metadata_is_never_discarded_for_nothing():
    """Swapping one title for a better one is fine; losing the only one is not."""
    data = make_pdf([["", "   ", "1"]], title="The Only Title There Is")
    assert read_pdf(data).title == "The Only Title There Is"


def test_a_title_that_wraps_is_joined():
    """"…Transformers for" plainly continues on the next line."""
    doc = read_pdf(make_pdf([[
        "BERT: Pre-training of Deep Bidirectional Transformers for",
        "Language Understanding",
        "Jacob Devlin", "Abstract", "Body."]]))
    assert doc.title == (
        "BERT: Pre-training of Deep Bidirectional Transformers for "
        "Language Understanding"
    )


def test_a_complete_title_does_not_swallow_the_author():
    doc = read_pdf(make_pdf([[
        "Attention Is All You Need", "Ashish Vaswani", "Abstract", "Body."]]))
    assert doc.title == "Attention Is All You Need"


def test_a_pdf_title_that_checks_out_is_kept():
    data = make_pdf(
        [["A Study Of Something Interesting", "Abstract", "Body text."]],
        title="A Study Of Something Interesting",
    )
    assert read_pdf(data).title == "A Study Of Something Interesting"


def test_a_browser_captured_pdf_drops_the_tab_title(cfg, db):
    """The tab title came from the viewer we just discarded."""
    from readcast.extract import looks_like_pdf

    shell = ('<html><head><link href="chrome-extension://x/pdf_embedder.css">'
             "</head><body></body></html>")
    assert looks_like_pdf(shell, "https://arxiv.org/pdf/1810.04805")

    job_id = db.create_job(
        id="2026-09-10-tabtitle", url="https://arxiv.org/pdf/1810.04805",
        title="BERT - arXiv", client_html=1,
    )
    directory = cfg.jobs_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raw.html").write_text(shell)

    # The worker clears both the flag and the borrowed title.
    db.update_job(job_id, client_html=0, title=None)
    assert db.get_job(job_id)["title"] is None


def test_re_extracting_corrects_a_title_that_extraction_got_wrong(cfg, db):
    """Improving the extractor has to be able to repair what it stored before."""
    from readcast.worker import RunOptions, run_job
    from tests.pdfbuild import make_pdf

    job_id = db.create_job(id="2026-09-10-retitle", url="https://example.org/p.pdf")
    directory = cfg.jobs_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    body = "We examine the question at some length and report what we found. "
    (directory / "raw.pdf").write_bytes(make_pdf([
        ["The Correct Title Of The Paper", "Abstract"] + [body + str(i) for i in range(6)],
        ["1 Introduction"] + [body + f"intro {i}" for i in range(6)],
    ]))
    db.update_job(job_id, title="A Truncated Title From")   # stored by an older run

    run_job(db, cfg, job_id, RunOptions(from_stage="extracting", until_stage="preparing"))
    assert db.get_job(job_id)["title"] == "The Correct Title Of The Paper"


def test_a_title_the_submitter_chose_is_kept(cfg, db):
    from readcast.worker import RunOptions, run_job
    from tests.pdfbuild import make_pdf

    job_id = db.create_job(
        id="2026-09-10-mytitle", url="https://example.org/p.pdf",
        title="What I Called It", title_locked=1,
    )
    directory = cfg.jobs_dir / job_id
    directory.mkdir(parents=True, exist_ok=True)
    body = "We examine the question at some length and report what we found. "
    (directory / "raw.pdf").write_bytes(make_pdf([
        ["The Paper's Own Title", "Abstract"] + [body + str(i) for i in range(6)],
        ["1 Introduction"] + [body + f"intro {i}" for i in range(6)],
    ]))
    run_job(db, cfg, job_id, RunOptions(from_stage="extracting", until_stage="preparing"))
    assert db.get_job(job_id)["title"] == "What I Called It"
