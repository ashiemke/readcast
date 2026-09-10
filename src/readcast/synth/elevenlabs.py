"""The paid reference backend."""

from __future__ import annotations

import os
from typing import Any

import httpx

from readcast.audio import to_wav_mono
from readcast.synth.openai_compat import SpeechError


class ElevenLabsBackend:
    name = "elevenlabs"
    supports_instruction = False
    supports_phonemes = False
    supports_reference = False

    def __init__(self, settings: dict[str, Any] | None = None):
        settings = dict(settings or {})
        self.settings = settings
        self.url = str(settings.get("url") or "https://api.elevenlabs.io/v1").rstrip("/")
        self.model = str(settings.get("model") or "eleven_turbo_v2_5")
        self.voice = str(settings.get("voice") or "21m00Tcm4TlvDq8ikWAM")
        self.timeout = float(settings.get("timeout_s", 300))
        self.api_key_env = str(settings.get("api_key_env") or "ELEVENLABS_API_KEY")
        self.cost_per_1k_chars = float(settings.get("cost_per_1k_chars", 0.30))

    def synth(
        self,
        text: str,
        *,
        voice: str = "",
        instruction: str | None = None,
        seed: int | None = None,
        reference: dict[str, Any] | None = None,
    ) -> bytes:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise SpeechError(f"environment variable {self.api_key_env} is not set")
        body: dict[str, Any] = {"text": text, "model_id": self.model}
        if seed is not None:
            body["seed"] = seed
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.url}/text-to-speech/{voice or self.voice}",
                    json=body,
                    headers={"xi-api-key": key, "content-type": "application/json"},
                    params={"output_format": "mp3_44100_128"},
                )
                if response.status_code >= 400:
                    raise SpeechError(
                        f"elevenlabs returned {response.status_code}: {response.text[:300]}"
                    )
                audio = response.content
        except httpx.HTTPError as exc:
            raise SpeechError(f"elevenlabs unreachable: {exc}") from exc
        return to_wav_mono(audio)
