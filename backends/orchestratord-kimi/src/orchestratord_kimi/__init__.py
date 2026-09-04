"""orchestratord-kimi — Kimi CLI backend.

Wraps the ``kimi`` binary as a spawn-per-turn Cli backend. Kimi is
friendly to Chinese-language prompts (FEATURE_GAP §8.1); the wire
format is treated as plain text until a stream-json variant is
exercised in-tree.
"""

from orchestratord_kimi.backend import KimiBackend

__all__ = ["KimiBackend"]