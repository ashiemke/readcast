"""The working document: text, a frozen mask, and a transform log.

The frozen mask is the one idea that keeps a rule pipeline honest. When a rule
replaces a span, the replacement is frozen and no later rule may match inside
it. Without it, a currency rule turns `$1.2M` into `one point two million
dollars` and a units rule then finds an `M` and edits the result again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass
class Transform:
    rule: str
    file: str
    offset: int
    before: str
    after: str

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "file": self.file,
            "offset": self.offset,
            "before": self.before,
            "after": self.after,
        }


@dataclass
class Reservation:
    """A span claimed early so no other rule can touch it.

    The lexicon reserves its matches before the builtins run. That is how
    `km/h` survives the generic slash rule and still becomes
    `kilometers per hour` at the lexicon step.
    """

    start: int
    end: int
    payload: object


@dataclass
class Doc:
    text: str
    frozen: bytearray = field(default_factory=bytearray)
    transforms: list[Transform] = field(default_factory=list)
    reservations: list[Reservation] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.frozen) != len(self.text):
            self.frozen = bytearray(len(self.text))

    # -- queries ------------------------------------------------------------

    def is_free(self, start: int, end: int) -> bool:
        return not any(self.frozen[start:end])

    # -- edits --------------------------------------------------------------

    def _splice(self, start: int, end: int, replacement: str, freeze: bool) -> None:
        self.text = self.text[:start] + replacement + self.text[end:]
        mark = 1 if freeze else 0
        self.frozen[start:end] = bytes([mark]) * len(replacement)
        delta = len(replacement) - (end - start)
        if delta:
            for res in self.reservations:
                if res.start >= end:
                    res.start += delta
                    res.end += delta

    def replace(
        self,
        start: int,
        end: int,
        replacement: str,
        *,
        rule: str,
        file: str,
        freeze: bool = True,
        log: bool = True,
    ) -> None:
        before = self.text[start:end]
        if log and before != replacement:
            self.transforms.append(Transform(rule, file, start, before, replacement))
        self._splice(start, end, replacement, freeze)

    def reserve(self, start: int, end: int, payload: object) -> None:
        self.frozen[start:end] = b"\x01" * (end - start)
        self.reservations.append(Reservation(start, end, payload))

    def apply(
        self,
        pattern: re.Pattern[str],
        repl: Callable[[re.Match[str]], str | None],
        *,
        rule: str,
        file: str,
        freeze: bool = True,
    ) -> int:
        """Apply one regex rule. Matches touching a frozen span are skipped.

        Replacements run right to left so earlier offsets stay valid.
        """
        edits: list[tuple[int, int, str]] = []
        for m in pattern.finditer(self.text):
            if m.start() == m.end() or not self.is_free(m.start(), m.end()):
                continue
            out = repl(m)
            if out is None:
                continue
            edits.append((m.start(), m.end(), out))
        for start, end, out in reversed(edits):
            self.replace(start, end, out, rule=rule, file=file, freeze=freeze)
        return len(edits)

    def delete_ranges(self, ranges: Iterable[tuple[int, int]]) -> None:
        for start, end in sorted(ranges, reverse=True):
            self._splice(start, end, "", freeze=False)

    # -- tidy ---------------------------------------------------------------

    _SPACE_RUN = re.compile(r"[ \t]{2,}")
    _SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.;:!?%)\]])")
    _SPACE_AFTER_OPEN = re.compile(r"([(\[])[ \t]+")
    _TRAILING_SPACE = re.compile(r"[ \t]+$", re.M)
    _LEADING_SPACE = re.compile(r"^[ \t]+", re.M)
    _BLANK_RUN = re.compile(r"\n{3,}")
    _EMPTY_PARENS = re.compile(r"\(\s*\)")
    _DOUBLE_PUNCT = re.compile(r"([,.;:])\s*\1+")

    def collapse_whitespace(self) -> None:
        """Run after every substitution. An empty group leaves a hole; close it.

        Mask-aware: a replacement keeps the frozen mark of the text it replaced,
        so tidying never thaws a span a rule has already claimed.
        """
        patterns = (
            (self._EMPTY_PARENS, ""),
            (self._SPACE_RUN, " "),
            (self._SPACE_BEFORE_PUNCT, r"\1"),
            (self._SPACE_AFTER_OPEN, r"\1"),
            (self._TRAILING_SPACE, ""),
            (self._LEADING_SPACE, ""),
            (self._BLANK_RUN, "\n\n"),
            (self._DOUBLE_PUNCT, r"\1"),
        )
        for _ in range(3):
            changed = False
            for pattern, template in patterns:
                edits = []
                for m in pattern.finditer(self.text):
                    repl = m.expand(template) if "\\" in template else template
                    if repl != m.group(0):
                        edits.append((m.start(), m.end(), repl))
                for start, end, repl in reversed(edits):
                    mark = bool(self.frozen[start])
                    self._splice(start, end, repl, freeze=mark)
                changed = changed or bool(edits)
            if not changed:
                return
