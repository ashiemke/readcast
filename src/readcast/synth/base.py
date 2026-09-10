from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TTSBackend(Protocol):
    name: str
    supports_instruction: bool
    supports_phonemes: bool

    def synth(
        self,
        text: str,
        *,
        voice: str,
        instruction: str | None = None,
        seed: int | None = None,
        reference: dict[str, Any] | None = None,
    ) -> bytes: ...   # 24 kHz mono WAV


def backend_names() -> list[str]:
    return ["mlx", "kokoro", "elevenlabs", "openai", "test"]


def get_backend(name: str, settings: dict[str, Any] | None = None) -> TTSBackend:
    settings = dict(settings or {})
    if name == "mlx":
        from readcast.synth.openai_compat import MLXBackend

        return MLXBackend(settings)
    if name == "kokoro":
        from readcast.synth.openai_compat import KokoroBackend

        return KokoroBackend(settings)
    if name == "openai":
        from readcast.synth.openai_compat import OpenAIBackend

        return OpenAIBackend(settings)
    if name == "elevenlabs":
        from readcast.synth.elevenlabs import ElevenLabsBackend

        return ElevenLabsBackend(settings)
    if name == "test":
        from readcast.synth.testing import TestBackend

        return TestBackend(settings)
    raise ValueError(f"unknown backend {name!r}; known: {', '.join(backend_names())}")
