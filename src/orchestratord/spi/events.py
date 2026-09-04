"""SPI: EventEnvelope — 规范化事件类型（Normalized Event Types）

The EventEnvelope stream is the **only** output channel from an
AgentSession.  Every backend-native event is mapped to one of these
kinds.  The stream is snapshot-tolerant: consumers may join at any
seq number and replay from the beginning.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(Enum):
    TEXT = "text"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TURN_COMPLETE = "turn_complete"
    PHASE_COMPLETE = "phase_complete"
    SESSION_COMPLETE = "session_complete"
    ERROR = "error"
    GOAL_SET = "goal_set"
    GOAL_STATUS = "goal_status"
    GOAL_CONTINUE = "goal_continue"
    GOAL_DONE = "goal_done"
    GOAL_CLEARED = "goal_cleared"
    GOAL_PAUSED = "goal_paused"
    # Emitted by approval_hooks backends when a tool call waits on a
    # human decision (DESIGN_chat_gateway.md §5.3).  Payload:
    # {request_id, call_id, tool_name, arguments}.
    APPROVAL_REQUEST = "approval_request"
    # Forward-compatible envelope for provider events not yet normalized.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EventEnvelope:
    """Normalized event emitted by an AgentSession.

    seq is monotonically increasing within a session (total order).
    Consumers that need to reconstruct state must be able to start
    from any seq and replay forward (snapshot-tolerant).
    """

    seq: int
    timestamp: float
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
