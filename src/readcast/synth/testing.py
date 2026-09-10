"""A backend that makes sound without a model.

It exists so the pipeline, the assembly step and the feed can be tested end to
end on a machine with no speech server. It is not a listening option.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from typing import Any

from readcast.audio import SAMPLE_RATE


def _wav(seconds: float, frequency: float = 180.0, sample_rate: int = SAMPLE_RATE) -> bytes:
    frames = int(seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        step = 2 * math.pi * frequency / sample_rate
        samples = bytearray()
        for i in range(frames):
            # A gentle envelope keeps the concatenation free of clicks.
            envelope = min(1.0, i / 240, (frames - i) / 240) if frames > 480 else 1.0
            samples += struct.pack("<h", int(9000 * envelope * math.sin(step * i)))
        out.writeframes(bytes(samples))
    return buf.getvalue()


class TestBackend:
    __test__ = False  # not a pytest class

    name = "test"
    supports_instruction = True
    supports_phonemes = False
    supports_reference = True
    cost_per_1k_chars = 0.0

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = dict(settings or {})
        self.chars_per_second = float(self.settings.get("chars_per_second", 15.0))
        self.calls: list[dict[str, Any]] = []

    def synth(
        self,
        text: str,
        *,
        voice: str = "default",
        instruction: str | None = None,
        seed: int | None = None,
        reference: dict[str, Any] | None = None,
    ) -> bytes:
        self.calls.append(
            {"text": text, "voice": voice, "instruction": instruction, "seed": seed,
             "reference": reference}
        )
        seconds = max(0.2, min(60.0, len(text) / self.chars_per_second))
        return _wav(seconds)
