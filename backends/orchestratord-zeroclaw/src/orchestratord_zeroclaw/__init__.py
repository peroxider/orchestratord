"""orchestratord-zeroclaw — ZeroClaw CLI backend.

Wraps the ``zeroclaw acp`` ACP (Agent Client Protocol) JSON-RPC 2.0
stdio transport as a spawn-per-turn Cli backend. FEATURE_GAP §8.2.3:
the session translates the real wire format — ``agent_message_chunk``
notifications stream as TEXT_DELTA, tool calls surface as
TOOL_CALL/TOOL_RESULT, and the turn ends on the ``session/prompt``
response.
"""

from orchestratord_zeroclaw.backend import ZeroclawBackend

__all__ = ["ZeroclawBackend"]
