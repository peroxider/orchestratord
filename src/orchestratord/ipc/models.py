"""IPC message models for the orchestratord IM gateway.

Plain dataclasses with no external dependencies for inbound and outbound
gateway messages.
"""

from __future__ import annotations

import time
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


class AckLayer(str, Enum):
    """Layered acknowledgement for inbound delivery."""

    ACCEPTED = "accepted"  # gateway received the inbound
    ENQUEUED = "enqueued"  # target host enqueued it
    PROCESSED = "processed"  # target runtime confirmed processing


# Wildcard origins for routing
WECHAT_DIRECT_ALL_ORIGIN = "wechat:direct:*:*"
FEISHU_DM_ALL_ORIGIN = "feishu:dm:*:*"
IM_DIRECT_ALL_ORIGIN = "im:direct:*:*"


@dataclass(frozen=True)
class OriginKey:
    """Unique inbound origin, e.g. ``wechat:direct:default:user_gz``."""

    value: str

    @classmethod
    def wechat(cls, account_id: str, from_user_id: str) -> OriginKey:
        return cls(f"wechat:direct:{account_id}:{from_user_id}")

    @classmethod
    def wechat_all_direct(cls) -> OriginKey:
        """All private/direct WeChat senders for the configured channel."""
        return cls(WECHAT_DIRECT_ALL_ORIGIN)

    @classmethod
    def feishu_all_dm(cls) -> OriginKey:
        """All private/direct Feishu DM senders for the configured channel."""
        return cls(FEISHU_DM_ALL_ORIGIN)

    @classmethod
    def im_all_direct(cls) -> OriginKey:
        """All supported private/direct IM senders."""
        return cls(IM_DIRECT_ALL_ORIGIN)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class SessionTarget:
    """Where an origin's inbound is routed."""

    session_id: str
    host_type: str = "default"  # default | repl | orchestrator


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
    # Plain ``str`` annotation, but a ``MessageSemantics`` (a ``str`` enum)
    # may be assigned as well; ``to_dict`` normalizes enum values.
    semantic: str | None = None
    context_token: str | None = None
    semantic_tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None
    channel: str = ""
    from_user_id: str | None = None
    received_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        semantic = self.semantic
        if isinstance(semantic, MessageSemantics):
            semantic = semantic.value
        return {
            "origin": self.origin,
            "text": self.text,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "channel_id": self.channel_id,
            "channel_type": self.channel_type,
            "message_id": self.message_id,
            "semantic": semantic,
            "context_token": self.context_token,
            "semantic_tags": list(self.semantic_tags),
            "metadata": dict(self.metadata) if self.metadata else None,
            "raw": dict(self.raw) if self.raw else None,
            "channel": self.channel,
            "from_user_id": self.from_user_id,
            "received_at": self.received_at,
        }


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "channel": self.channel,
            "level": self.level,
            "markdown": self.markdown,
            "title": self.title,
            "target": self.target,
            "context_token": self.context_token,
            "semantic_tags": list(self.semantic_tags),
            "metadata": dict(self.metadata) if self.metadata else None,
            "idempotency_key": self.idempotency_key,
        }


@dataclass
class AckReceipt:
    """Layered ack returned to the inbound caller."""

    delivery_id: str
    layer: str | AckLayer  # "accepted" | "enqueued" | "processed"
    message: str = ""
    notify_user: bool = False

    def to_dict(self) -> dict[str, Any]:
        layer = self.layer.value if isinstance(self.layer, AckLayer) else self.layer
        return {
            "delivery_id": self.delivery_id,
            "layer": layer,
            "message": self.message,
            "notify_user": self.notify_user,
        }
