"""orchestratord-kimi — Kimi CLI backend.

Wraps the ``kimi`` binary as a spawn-per-turn backend. Ported from
multica ``server/pkg/agent/kimi.go``: Kimi Code CLI speaks ACP
(Agent Client Protocol) JSON-RPC 2.0 over stdio via the ``acp``
subcommand, so the session translates ``session/update`` streams into
real TEXT_DELTA / TOOL_CALL / TOOL_RESULT events (FEATURE_GAP §8.2.3).
"""

from orchestratord_kimi.backend import KimiBackend

__all__ = ["KimiBackend"]