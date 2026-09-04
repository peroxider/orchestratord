"""orchestratord-qwen — Qwen (DashScope) CLI backend.

Wraps the ``qwen`` binary in ``--output-format stream-json`` mode as
a spawn-per-turn Cli backend with real TEXT_DELTA streaming.
"""

from orchestratord_qwen.backend import QwenBackend

__all__ = ["QwenBackend"]