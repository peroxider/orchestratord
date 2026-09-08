"""orchestratord-reasonix — Reasonix CLI backend.

Wraps the ``reasonix`` binary as a spawn-per-turn backend. Ported from
multica ``server/pkg/agent/reasonix.go``: the Reasonix CLI speaks ACP
(Agent Client Protocol) JSON-RPC 2.0 over stdio via the ``acp``
subcommand, so the session translates ``session/update`` streams into
real TEXT_DELTA / TOOL_CALL / TOOL_RESULT events (FEATURE_GAP §8.2.3).
"""

from orchestratord_reasonix.backend import ReasonixBackend

__all__ = ["ReasonixBackend"]
