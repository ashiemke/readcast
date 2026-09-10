"""The text preparation stage, run once, in order.

1. Structural strip   2. Pattern strip   3. Pre-builtin rules
4. Builtins           5. Post-builtin rules   6. Lexicon

Outputs spoken.txt (what the engine hears), transforms.jsonl (which rule did
that), and unknowns.jsonl (what to tune next).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readcast.prepare.builtins import apply_builtins
from readcast.prepare.document import Doc
from readcast.prepare.lexicon import apply_lexicon, build_terms, reserve_terms
from readcast.prepare.loader import RuleSet, host_of, load_rules
from readcast.prepare.rules import apply_rules, compile_flags
from readcast.prepare.structural import structural_strip
from readcast.prepare.unknowns import find_unknowns


@dataclass
class PrepareResult:
    spoken: str
    transforms: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\b[\w'’-]+\b", self.spoken))

    def write(self, job_dir: str | Path) -> None:
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "spoken.txt").write_text(self.spoken)
        with (job_dir / "transforms.jsonl").open("w") as fh:
            for row in self.transforms:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with (job_dir / "unknowns.jsonl").open("w") as fh:
            for row in self.unknowns:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _pattern_strip(doc: Doc, patterns: list[dict[str, Any]]) -> None:
    for entry in patterns:
        if entry.get("enabled", True) is False or not entry.get("match"):
            continue
        try:
            pattern = re.compile(entry["match"], compile_flags(entry.get("flags")))
        except re.error:
            continue
        doc.apply(
            pattern,
            lambda m: "",
            rule=str(entry.get("id", entry["match"])),
            file="strip.yml",
            freeze=False,
        )
        doc.collapse_whitespace()


def _tidy(text: str) -> str:
    """Final shape of spoken.txt: a blank line at every paragraph break."""
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        # A blank line before a heading, and before the *start* of a quote
        # block — consecutive quote lines are one block, not one each.
        starts_quote = line.startswith("> ") and not (out and out[-1].startswith("> "))
        if (line.startswith("##") or starts_quote) and out and out[-1] != "":
            out.append("")
        out.append(line)
        if line.startswith("##"):
            out.append("")
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out).strip() + "\n"


def prepare_text(
    text: str,
    ruleset: RuleSet,
    *,
    host: str | None = None,
    supports_phonemes: bool = False,
    collect_unknowns: bool = True,
) -> PrepareResult:
    structural_log: list[dict[str, Any]] = []
    stripped = structural_strip(text, ruleset.structural, structural_log)

    doc = Doc(stripped)
    doc.transforms.extend([])  # structural entries are merged in below
    _pattern_strip(doc, ruleset.strip_patterns)

    terms = build_terms(ruleset.lexicon_defaults, ruleset.lexicon_terms)
    reserve_terms(doc, terms)

    apply_rules(doc, ruleset.phase("pre_builtin"), host)
    apply_builtins(doc, ruleset.builtins)
    apply_rules(doc, ruleset.phase("post_builtin"), host)
    apply_lexicon(doc, terms, supports_phonemes)

    spoken = _tidy(doc.text)
    transforms = structural_log + [t.as_dict() for t in doc.transforms]

    unknowns: list[dict[str, Any]] = []
    if collect_unknowns:
        known = {t.match.lower() for t in terms}
        known |= {w for t in terms for w in t.match.lower().split()}
        unknowns = find_unknowns(spoken, known)

    return PrepareResult(spoken=spoken, transforms=transforms, unknowns=unknowns)


def prepare_from_rules_dir(
    text: str, rules_dir: str | Path, url: str | None = None, **kwargs: Any
) -> PrepareResult:
    ruleset = load_rules(rules_dir, url)
    return prepare_text(text, ruleset, host=host_of(url) if url else None, **kwargs)
