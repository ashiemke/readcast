"""Operator regex rules from normalize.yml, phases `pre_builtin` and
`post_builtin`.

Fields: id, phase, match, flags, say, expand, map, enabled, note, scope.
Group references in `say` use `$1` style. `$1_EXPANDED` looks the group up in
`expand`. `say: MAP` looks the whole match up in `map`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from readcast.prepare.document import Doc

log = logging.getLogger("readcast.rules")
FILE = "normalize.yml"

GROUP_REF = re.compile(r"\$(\d+)(_EXPANDED)?")

FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A}


def compile_flags(spec: Any) -> int:
    flags = 0
    for ch in str(spec or ""):
        flags |= FLAG_MAP.get(ch.lower(), 0)
    return flags


def in_scope(rule: dict[str, Any], host: str | None) -> bool:
    scope = rule.get("scope")
    if not scope:
        return True
    hosts = [scope] if isinstance(scope, str) else list(scope)
    return any((host or "").endswith(str(h).lower().lstrip("*.")) for h in hosts)


def _render(rule: dict[str, Any], m: re.Match[str]) -> str | None:
    say = rule.get("say")
    if say is None:
        return ""  # a rule with no `say` deletes the match
    if say == "MAP":
        table = rule.get("map") or {}
        whole = m.group(0)
        if whole in table:
            return str(table[whole])
        for key, value in table.items():
            if key.lower() == whole.lower():
                return str(value)
        return None
    expand = rule.get("expand") or {}

    def sub(ref: re.Match[str]) -> str:
        idx = int(ref.group(1))
        try:
            value = m.group(idx) or ""
        except (IndexError, re.error):
            return ""
        if ref.group(2):  # $1_EXPANDED
            return str(expand.get(value, value))
        return value

    return GROUP_REF.sub(sub, str(say))


def apply_rules(doc: Doc, rules: list[dict[str, Any]], host: str | None = None) -> None:
    for rule in rules:
        if not in_scope(rule, host):
            continue
        pattern_src = rule.get("match")
        if not pattern_src:
            continue
        rule_id = str(rule.get("id", pattern_src))
        try:
            pattern = re.compile(pattern_src, compile_flags(rule.get("flags")))
        except re.error as exc:
            log.warning("rule %s has an invalid pattern: %s", rule_id, exc)
            continue
        doc.apply(pattern, lambda m, r=rule: _render(r, m), rule=rule_id, file=FILE)
        doc.collapse_whitespace()
