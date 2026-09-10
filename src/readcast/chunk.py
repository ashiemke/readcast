"""Stage 4: chunking.

Long inputs make speech models drop or invent words. Small chunks bound that
damage and make a retry cheap. Split on sentence boundaries only; never split
inside a sentence, never merge across a heading.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

# After preparation most abbreviations are already words. These survive.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "inc", "ltd", "co",
    "corp", "dept", "est", "fig", "no", "vol", "approx", "min", "max",
}

SPEAKABLE = re.compile(r"[A-Za-z0-9]")
SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)\]]*(\s+)")
HEADING = re.compile(r"^##\s+(.*)$")
QUOTE_LINE = re.compile(r"^>\s?", re.M)
# A paragraph wrapped entirely in parentheses is an aside, not narration.
ASIDE = re.compile(r"^\((.*)\)[.]?$", re.S)


@dataclass
class Chunk:
    index: int
    text: str
    kind: str                 # intro | heading | body | quote | aside
    ends_paragraph: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in SENTENCE_END.finditer(text):
        end = m.start()
        candidate = text[start:end].strip()
        if not candidate:
            continue
        last = re.split(r"[\s(]", candidate)[-1].rstrip(".").lower()
        if last in ABBREVIATIONS:
            continue
        # A single capital before the period reads as an initial: "J. Smith".
        if re.search(r"(?:^|\s)[A-Z]\.$", candidate):
            continue
        out.append(candidate)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _pack(sentences: Iterable[str], target: int, maximum: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
            continue
        if len(current) + 1 + len(sentence) <= maximum and len(current) < target:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def plan_chunks(
    spoken: str,
    *,
    target_chars: int = 300,
    max_chars: int = 450,
    intro: str | None = None,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    if intro and SPEAKABLE.search(intro):
        chunks.append(Chunk(index=0, text=" ".join(intro.split()), kind="intro"))

    for block in re.split(r"\n\s*\n", spoken):
        block = block.strip()
        if not block:
            continue
        heading = HEADING.match(block)
        if heading:
            title = heading.group(1).strip()
            if title and SPEAKABLE.search(title):
                chunks.append(Chunk(index=len(chunks), text=title, kind="heading"))
            continue

        kind = "body"
        if block.startswith(">"):
            # The marker is for the operator and the chunker; it is never spoken.
            block = QUOTE_LINE.sub("", block).strip()
            kind = "quote"
        else:
            aside = ASIDE.match(block.strip())
            if aside and aside.group(1).strip():
                block = aside.group(1).strip()
                kind = "aside"
        if not block:
            continue

        packed = [t for t in _pack(split_sentences(block), target_chars, max_chars)
                  if SPEAKABLE.search(t)]
        for i, text in enumerate(packed):
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=text,
                    kind=kind,
                    ends_paragraph=(i == len(packed) - 1),
                )
            )
    for i, chunk in enumerate(chunks):
        chunk.index = i
    return chunks


def write_plan(chunks: list[Chunk], job_dir: str | Path) -> Path:
    path = Path(job_dir) / "chunks" / "plan.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk.as_dict(), ensure_ascii=False) + "\n")
    return path


def read_plan(job_dir: str | Path) -> list[Chunk]:
    path = Path(job_dir) / "chunks" / "plan.jsonl"
    if not path.is_file():
        return []
    return [Chunk(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
