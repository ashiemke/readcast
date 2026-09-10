"""Load rules from disk and merge the per-domain overrides.

Merge order: global, then domain. A domain entry with the same `id` (patterns
and rules) or the same `match` (lexicon terms) replaces the global entry.
Every override that fires is logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

log = logging.getLogger("readcast.rules")


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class RuleSet:
    structural: dict[str, Any] = field(default_factory=dict)
    strip_patterns: list[dict[str, Any]] = field(default_factory=list)
    builtins: dict[str, Any] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)
    lexicon_defaults: dict[str, Any] = field(default_factory=dict)
    lexicon_terms: list[dict[str, Any]] = field(default_factory=list)
    extract: dict[str, Any] = field(default_factory=dict)
    domain: str | None = None

    def phase(self, name: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rules
            if r.get("enabled", True) and r.get("phase", "post_builtin") == name
        ]


def _merge_by(
    base: list[dict[str, Any]], over: list[dict[str, Any]], key: str, what: str
) -> list[dict[str, Any]]:
    out = list(base)
    index = {item.get(key): i for i, item in enumerate(out) if item.get(key) is not None}
    for item in over:
        ident = item.get(key)
        if ident is not None and ident in index:
            log.info("domain override replaces %s %r", what, ident)
            out[index[ident]] = item
        else:
            log.info("domain override adds %s %r", what, ident)
            out.append(item)
    return out


def load_rules(rules_dir: str | Path, url: str | None = None) -> RuleSet:
    rules_dir = Path(rules_dir)
    strip = _read(rules_dir / "strip.yml")
    normalize = _read(rules_dir / "normalize.yml")
    lexicon = _read(rules_dir / "lexicon.yml")

    rs = RuleSet(
        structural=dict(strip.get("structural") or {}),
        strip_patterns=list(strip.get("patterns") or []),
        builtins=dict(normalize.get("builtins") or {}),
        rules=list(normalize.get("rules") or []),
        lexicon_defaults=dict(lexicon.get("defaults") or {}),
        lexicon_terms=list(lexicon.get("terms") or []),
    )

    host = host_of(url) if url else ""
    if not host:
        return rs
    for candidate in (host, f"www.{host}"):
        path = rules_dir / "domains" / f"{candidate}.yml"
        if not path.is_file():
            continue
        dom = _read(path)
        log.info("applying domain rules from %s", path.name)
        rs.domain = candidate
        rs.extract = dict(dom.get("extract") or {})
        dstrip = dom.get("strip") or {}
        rs.structural |= dict(dstrip.get("structural") or {})
        rs.strip_patterns = _merge_by(
            rs.strip_patterns, list(dstrip.get("patterns") or []), "id", "strip pattern"
        )
        dnorm = dom.get("normalize") or {}
        for group, values in (dnorm.get("builtins") or {}).items():
            merged = dict(rs.builtins.get(group) or {})
            merged.update(values or {})
            rs.builtins[group] = merged
        rs.rules = _merge_by(rs.rules, list(dnorm.get("rules") or []), "id", "rule")
        dlex = dom.get("lexicon") or {}
        rs.lexicon_defaults |= dict(dlex.get("defaults") or {})
        rs.lexicon_terms = _merge_by(
            rs.lexicon_terms, list(dlex.get("terms") or []), "match", "lexicon term"
        )
        break
    return rs
