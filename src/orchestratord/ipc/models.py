"""IPC message models for the orchestratord IM gateway.

Plain dataclasses with no external dependencies for inbound and outbound
gateway messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageSemantics(str, Enum):
    """Message semantics classification."""
    NEW_PROMPT = "newPrompt"
    COMMAND = "command"
    FOLLOW_UP = "followUp"
    APPROVAL = "approval"
    INTERRUPT = "interrupt"
    CONTEXT_ONLY = "contextOnly"
    UNSUPPORTED_MEDIA = "unsupportedMedia"


# Wildcard origins for routing
FEISHU_DM_ALL_ORIGIN = "feishu:dm:*:*"
IM_DIRECT_ALL_ORIGIN = "im:direct:*:*"


@dataclass
class InboundMessage:
    """Normalized inbound message from a channel adapter."""

    origin: str
    text: str
    sender_id: str | None = None
    sender_name: str | None = None
    channel_id: str | None = None
    channel_type: str = ""  # "feishu" | "slack" | "cli"
    message_id: str = ""
    semantic: str | None = None
    context_token: str | None = None
    semantic_tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


@dataclass
class OutboundMessage:
    """Outbound message to send to a channel."""

    text: str
    channel: str = ""
    level: str = "info"  # "info" | "success" | "warn" | "error"
    markdown: bool = True
    title: str | None = None
    target: str | None = None
    context_token: str | None = None
    semantic_tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] | None = None
    idempotency_key: str | None = None


@dataclass
class AckReceipt:
    """Layered ack returned to the inbound caller."""

    delivery_id: str
    layer: str  # "accepted" | "enqueued" | "processed"
    message: str = ""
    notify_user: bool = False
