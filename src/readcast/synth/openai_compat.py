"""Backends that speak the OpenAI `/v1/audio/speech` shape.

`mlx-audio` serves it locally, `kokoro` serves it on another port, and OpenAI
itself serves it over the network. One client covers all three.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from readcast.audio import to_wav_mono


class SpeechError(RuntimeError):
    pass


class OpenAICompatBackend:
    name = "openai-compatible"
    supports_instruction = False
    supports_phonemes = False
    default_url = "http://127.0.0.1:8080/v1"
    default_model = ""
    default_voice = "default"
    cost_per_1k_chars = 0.0
    supports_seed = True
    #: Can the speaker be pinned with a reference clip, or only by name?
    supports_reference = False

    def __init__(self, settings: dict[str, Any] | None = None):
        settings = dict(settings or {})
        self.settings = settings
        self.url = str(settings.get("url") or self.default_url).rstrip("/")
        self.model = str(settings.get("model") or self.default_model)
        self.voice = str(settings.get("voice") or self.default_voice)
        self.timeout = float(settings.get("timeout_s", 300))
        self.cost_per_1k_chars = float(
            settings.get("cost_per_1k_chars", self.cost_per_1k_chars)
        )
        self._api_key_env = settings.get("api_key_env")

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key_env:
            key = os.environ.get(str(self._api_key_env))
            if not key:
                raise SpeechError(
                    f"{self.name}: environment variable {self._api_key_env} is not set"
                )
            headers["authorization"] = f"Bearer {key}"
        return headers

    #: Field the server reads the per-chunk instruction from.
    instruction_field = "instructions"
    #: Sampling knobs passed straight through when the backend supports them.
    sampling_keys: tuple[str, ...] = ()

    def _payload(
        self,
        text: str,
        voice: str,
        instruction: str | None,
        seed: int | None,
        reference: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": voice or self.voice,
            "response_format": "wav",
        }
        if instruction and self.supports_instruction:
            body[self.instruction_field] = instruction
        if seed is not None and self.supports_seed:
            body["seed"] = seed
        if reference:
            body.update(reference)
        for key in self.sampling_keys:
            if key in self.settings:
                body[key] = self.settings[key]
        return body

    def synth(
        self,
        text: str,
        *,
        voice: str = "",
        instruction: str | None = None,
        seed: int | None = None,
        reference: dict[str, Any] | None = None,
    ) -> bytes:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.url}/audio/speech",
                    json=self._payload(text, voice, instruction, seed, reference),
                    headers=self._headers(),
                )
                if response.status_code >= 400:
                    raise SpeechError(
                        f"{self.name} returned {response.status_code}: "
                        f"{response.text[:300]}"
                    )
                audio = response.content
        except httpx.HTTPError as exc:
            raise SpeechError(
                f"{self.name} unreachable at {self.url}: {exc}. "
                "Is the speech server running?"
            ) from exc
        if not audio:
            raise SpeechError(f"{self.name} returned an empty body")
        return to_wav_mono(audio)


class MLXBackend(OpenAICompatBackend):
    name = "mlx"
    supports_instruction = True
    default_url = "http://127.0.0.1:8080/v1"
    default_model = "mlx-community/Breeze-TTS-2-mlx"
    # mlx-audio declares `instruct`, not OpenAI's `instructions`, and has no
    # seed at all. Sending the wrong names is silently ignored, which is how
    # the per-chunk instructions did nothing for so long.
    instruction_field = "instruct"
    supports_seed = False
    supports_reference = True
    sampling_keys = ("temperature", "top_p", "top_k", "repetition_penalty")


class KokoroBackend(OpenAICompatBackend):
    name = "kokoro"
    supports_seed = False
    supports_instruction = False
    default_url = "http://127.0.0.1:8081/v1"
    default_model = "kokoro"
    default_voice = "af_heart"


class OpenAIBackend(OpenAICompatBackend):
    name = "openai"
    supports_instruction = True
    default_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini-tts"
    default_voice = "alloy"
    cost_per_1k_chars = 0.015

    def __init__(self, settings: dict[str, Any] | None = None):
        settings = dict(settings or {})
        settings.setdefault("api_key_env", "OPENAI_API_KEY")
        super().__init__(settings)
