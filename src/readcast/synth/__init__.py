"""Speech backends behind one protocol.

The operator switches backends with one line in config.yml. Nothing else in the
pipeline knows which engine produced the audio.
"""

from readcast.synth.base import TTSBackend, backend_names, get_backend

__all__ = ["TTSBackend", "get_backend", "backend_names"]
