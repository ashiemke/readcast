from __future__ import annotations

from readcast.chunk import Chunk, plan_chunks, read_plan, split_sentences, write_plan

TEXT = """## First section

One sentence here. A second sentence follows, and it is somewhat longer than the
first one was. A third arrives.

Another paragraph entirely.

## Second section

Short.
"""


def test_headings_become_their_own_chunks():
    chunks = plan_chunks(TEXT)
    kinds = [c.kind for c in chunks]
    assert kinds[0] == "heading"
    assert chunks[0].text == "First section"
    assert "##" not in " ".join(c.text for c in chunks)


def test_never_merges_across_a_heading():
    chunks = plan_chunks(TEXT, target_chars=10_000, max_chars=10_000)
    for chunk in chunks:
        if chunk.kind == "body":
            assert "First section" not in chunk.text
            assert "Second section" not in chunk.text


def test_never_splits_inside_a_sentence():
    sentences = [f"Sentence number {i} is here." for i in range(40)]
    chunks = plan_chunks(" ".join(sentences))
    rebuilt = " ".join(c.text for c in chunks)
    for sentence in sentences:
        assert sentence in rebuilt


def test_chunks_stay_under_the_maximum():
    body = " ".join(f"This is sentence {i}, of a fairly ordinary length." for i in range(200))
    chunks = plan_chunks(body, target_chars=300, max_chars=450)
    assert all(len(c.text) <= 450 for c in chunks)
    assert sum(len(c.text) for c in chunks) > 1000
    # Most chunks should land in the target band rather than at the floor.
    body_chunks = [c for c in chunks if c.kind == "body"]
    assert sum(1 for c in body_chunks if len(c.text) >= 200) >= len(body_chunks) - 1


def test_intro_is_the_first_chunk():
    chunks = plan_chunks(TEXT, intro="A title. From Somewhere.")
    assert chunks[0].kind == "intro"
    assert chunks[0].text == "A title. From Somewhere."


def test_abbreviations_do_not_end_a_sentence():
    assert split_sentences("Dr. Smith went home. He slept.") == [
        "Dr. Smith went home.",
        "He slept.",
    ]


def test_plan_round_trips_through_disk(tmp_path):
    chunks = plan_chunks(TEXT, intro="Intro line.")
    write_plan(chunks, tmp_path)
    again = read_plan(tmp_path)
    assert [c.as_dict() for c in again] == [c.as_dict() for c in chunks]
    assert isinstance(again[0], Chunk)


def test_paragraph_ends_are_marked_for_the_pauses():
    chunks = plan_chunks(TEXT)
    body = [c for c in chunks if c.kind == "body"]
    assert body[-1].ends_paragraph
