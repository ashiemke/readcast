"""Chunk verification.

Word dropping is the failure mode that ruins a listen, and it is silent.
Transcribe what the engine actually said, compare it to what it was given, and
retry the chunk when they diverge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("readcast.verify")

PUNCT = re.compile(r"[^\w\s]")

# Whisper writes numbers as numerals: it hears "thirty-eight percent" and
# transcribes "38%". The text we sent was already spelled out, so a raw
# comparison scores every number as an error and re-synthesizes good audio.
# Put both sides through the same number handling first.
VERIFY_BUILTINS = {
    "urls": {"enabled": False},
    "dates": {"enabled": True, "style": "month_day_year"},
    "currency": {
        "enabled": True,
        "magnitudes": {"K": "thousand", "M": "million", "B": "billion", "T": "trillion"},
    },
    "ranges": {"enabled": True, "joiner": " to "},
    "percent": {"enabled": True},
    "units": {"enabled": False},
    "numbers": {"enabled": True, "max_digits_spelled": 9, "year_style": "pairs",
                "ordinals": True},
    "symbols": {"enabled": True, "table": {"&": "and", "%": "percent", "~": "approximately"}},
}


def spell_numbers(text: str) -> str:
    """Run the number builtins over a string. Idempotent on spelled-out text."""
    from readcast.prepare.builtins import apply_builtins
    from readcast.prepare.document import Doc

    doc = Doc(text)
    try:
        apply_builtins(doc, VERIFY_BUILTINS)
    except Exception:  # noqa: BLE001 - a comparison must never fail the job
        return text
    return doc.text


def normalize(text: str) -> str:
    return " ".join(PUNCT.sub(" ", spell_numbers(text).lower()).split())


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,          # deletion
                    current[j - 1] + 1,       # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return 0.0
    return edit_distance(ref, hyp) / len(ref)


@dataclass
class VerifyResult:
    cer: float
    transcript: str
    ok: bool
    available: bool = True


@lru_cache(maxsize=2)
def _whisper():
    import mlx_whisper  # noqa: PLC0415 - optional extra, imported on demand

    return mlx_whisper


def transcribe(path: str | Path, model: str) -> str:
    whisper = _whisper()
    result = whisper.transcribe(str(path), path_or_hf_repo=model, verbose=False)
    return str(result.get("text", ""))


def verify_chunk(
    path: str | Path, text: str, *, model: str, max_cer: float = 0.15
) -> VerifyResult:
    try:
        transcript = transcribe(path, model)
    except ImportError:
        log.warning(
            "mlx-whisper is not installed; skipping verification. "
            "Install it with `uv sync --extra mlx` or set verify.enabled to false."
        )
        return VerifyResult(cer=0.0, transcript="", ok=True, available=False)
    except Exception as exc:  # noqa: BLE001 - a broken check must not fail the job
        log.warning("verification failed for %s: %s", path, exc)
        return VerifyResult(cer=0.0, transcript="", ok=True, available=False)
    rate = character_error_rate(text, transcript)
    return VerifyResult(cer=rate, transcript=transcript, ok=rate <= max_cer)
