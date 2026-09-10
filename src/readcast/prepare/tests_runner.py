"""`readcast rules test` — the cases in rules/tests.yml, plus golden files.

Exit non-zero on any failure. Print a unified diff for each failure.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from readcast.prepare.loader import load_rules
from readcast.prepare.pipeline import prepare_text


@dataclass
class CaseResult:
    name: str
    passed: bool
    expected: str
    actual: str

    def diff(self) -> str:
        return "\n".join(
            difflib.unified_diff(
                self.expected.splitlines(),
                self.actual.splitlines(),
                fromfile="expected",
                tofile="actual",
                lineterm="",
            )
        )


def run_cases(rules_dir: str | Path) -> list[CaseResult]:
    rules_dir = Path(rules_dir)
    spec = yaml.safe_load((rules_dir / "tests.yml").read_text()) or {}
    ruleset = load_rules(rules_dir)
    results: list[CaseResult] = []
    for case in spec.get("cases") or []:
        expected = str(case.get("out", "")).strip()
        result = prepare_text(str(case.get("in", "")), ruleset, collect_unknowns=False)
        actual = result.spoken.strip()
        results.append(
            CaseResult(str(case.get("name", "unnamed")), actual == expected, expected, actual)
        )
    return results


def format_report(results: list[CaseResult]) -> tuple[str, int]:
    lines, failed = [], 0
    for r in results:
        if r.passed:
            lines.append(f"  ok    {r.name}")
        else:
            failed += 1
            lines.append(f"  FAIL  {r.name}")
            lines.append("        expected: " + r.expected)
            lines.append("        actual:   " + r.actual)
    lines.append("")
    lines.append(f"{len(results) - failed} passed, {failed} failed")
    return "\n".join(lines), failed
