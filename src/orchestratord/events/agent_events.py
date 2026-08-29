"""Backend-neutral agent event types.

These dataclasses define the backend-neutral event contract consumed by
the orchestration core.  Backends translate their native protocol into
these values; optional fields default to ``None`` / empty so a backend
that does not emit an attribute can still produce a valid event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Streaming events (text + tool lifecycle)
# ---------------------------------------------------------------------------


@dataclass
class TextDelta:
    """Incremental text chunk emitted by the agent's model layer."""

    content: str = ""


@dataclass
class ToolCallEvent:
    """The agent decided to invoke a tool.

    ``params`` mirrors the JSON object passed to the tool.  ``_approved``
    is set by the approval-policy layer (None when no policy applies).
    """

    tool_name: str = ""
    tool_use_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    _approved: bool | None = None


@dataclass
class ToolResultEvent:
    """A tool finished executing."""

    tool_name: str = ""
    tool_use_id: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lifecycle events (phase / turn / session)
# ---------------------------------------------------------------------------


@dataclass
class PhaseComplete:
    """A logical phase finished.

    The orchestrator does not interpret ``phase`` numerically — it is
    opaque to the orchestrator and surfaced to sinks verbatim. Backends
    are free to use numbers or strings appropriate to their protocol.
    """

    phase: Any = 0
    turn_count: int = 0
    progress: float | None = None


@dataclass
class TurnComplete:
    """A single LLM turn finished."""

    turn: int = 0


@dataclass
class SessionComplete:
    """The whole session ended (success, error, or operator interrupt)."""

    reason: str = ""


# ---------------------------------------------------------------------------
# Generic envelope (fallback for backends that emit dict events)
# ---------------------------------------------------------------------------


@dataclass
class EventEnvelope:
    """Generic envelope used by SPI backends that emit dict payloads.

    ``kind`` is one of the ``EventKind`` strings (``text_delta``,
    ``tool_call``, ``tool_result``, ``phase_complete``, ``turn_complete``,
    ``session_complete``).  ``payload`` is the per-event dict.
    """

    kind: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Message blocks (used by transcript inject / takeover REPL)
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str = ""


@dataclass
class ToolUseBlock:
    id: str | None = None
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    tool_use_id: str | None = None
    content: Any = ""
    is_error: bool = False


@dataclass
class UserMessage:
    """A user-originated message for the transcript."""

    content: list[Any] = field(default_factory=list)
    origin: str = ""


@dataclass
class AssistantMessage:
    """An assistant-originated message for the transcript."""

    content: list[Any] = field(default_factory=list)


def create_user_message(content: list[Any], origin: str = "") -> UserMessage:
    """Construct a :class:`UserMessage` from a list of content blocks."""
    return UserMessage(content=list(content), origin=origin)


def create_assistant_message(content: list[Any]) -> AssistantMessage:
    """Construct an :class:`AssistantMessage` from a list of content blocks."""
    return AssistantMessage(content=list(content))


__all__ = [
    # streaming
    "TextDelta",
    "ToolCallEvent",
    "ToolResultEvent",
    # lifecycle
    "PhaseComplete",
    "TurnComplete",
    "SessionComplete",
    # generic
    "EventEnvelope",
    # message blocks
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "UserMessage",
    "AssistantMessage",
    "create_user_message",
    "create_assistant_message",
]
