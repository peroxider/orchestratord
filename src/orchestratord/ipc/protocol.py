"""Gateway IPC frame protocol.

JSONL over UDS with frame types: REGISTER, HEARTBEAT, DELIVER, ACK,
NACK, EVENT, OUTBOUND.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PROTOCOL_VERSION = "gateway/1"
CHANNEL_RELOAD_TIMEOUT_SECONDS = 60.0


class FrameType(str, Enum):
    REGISTER = "register"
    UNREGISTER = "unregister"
    HEARTBEAT = "heartbeat"
    DELIVER = "deliver"
    ACK = "ack"
    NACK = "nack"
    EVENT = "event"
    OUTBOUND = "outbound"


@dataclass
class GatewayFrame:
    type: FrameType
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    protocol_version: str = PROTOCOL_VERSION
    session_id: str | None = None
    origin: str | None = None
    delivery_id: str | None = None
    capabilities: list[str] = field(default_factory=list)
    token: str | None = None
    text: str | None = None
    semantic: str | None = None
    ack_layer: str | None = None
    reason: str | None = None
    event_type: str | None = None
    deadline_ms: int | None = None
    payload: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    semantic_tags: list[str] = field(default_factory=list)
    context_token: str | None = None
    in_reply_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": self.type.value,
            "message_id": self.message_id,
            "protocol_version": self.protocol_version,
        }
        for k in (
            "session_id", "origin", "delivery_id", "capabilities",
            "token", "text", "semantic", "ack_layer", "reason",
            "event_type", "deadline_ms", "payload", "metadata",
            "semantic_tags", "context_token", "in_reply_to",
        ):
            v = getattr(self, k)
            if v not in (None, [], {}):
                d[k] = v
        return d

    def encode(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes | str) -> GatewayFrame:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayFrame:
        if not isinstance(data, dict):
            raise ValueError("frame must be a JSON object")  # noqa: TRY004
        ftype = data.get("type")
        if ftype is None:
            raise ValueError("frame missing 'type'")
        try:
            ftype_enum = FrameType(ftype)
        except ValueError as exc:
            raise ValueError(f"unknown frame type {ftype!r}") from exc
        return cls(
            type=ftype_enum,
            message_id=data.get("message_id", str(uuid.uuid4())),
            protocol_version=data.get("protocol_version", PROTOCOL_VERSION),
            session_id=data.get("session_id"),
            origin=data.get("origin"),
            delivery_id=data.get("delivery_id"),
            capabilities=list(data.get("capabilities") or []),
            token=data.get("token"),
            text=data.get("text"),
            semantic=data.get("semantic"),
            ack_layer=data.get("ack_layer"),
            reason=data.get("reason"),
            event_type=data.get("event_type"),
            deadline_ms=data.get("deadline_ms"),
            payload=data.get("payload"),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else None,
            semantic_tags=list(data.get("semantic_tags") or []),
            context_token=data.get("context_token"),
            in_reply_to=data.get("in_reply_to"),
        )

    # -- convenience constructors --

    @classmethod
    def register(cls, *, session_id: str, origin: str,
                 capabilities: list[str] | None = None,
                 token: str | None = None) -> GatewayFrame:
        return cls(type=FrameType.REGISTER, session_id=session_id,
                   origin=origin, capabilities=list(capabilities or []), token=token)

    @classmethod
    def heartbeat(cls, *, session_id: str) -> GatewayFrame:
        return cls(type=FrameType.HEARTBEAT, session_id=session_id)

    @classmethod
    def deliver(cls, *, delivery_id: str, session_id: str, origin: str,
                text: str, semantic: str | None = None,
                deadline_ms: int | None = None,
                context_token: str | None = None,
                metadata: dict[str, Any] | None = None) -> GatewayFrame:
        return cls(type=FrameType.DELIVER, delivery_id=delivery_id,
                   session_id=session_id, origin=origin, text=text,
                   semantic=semantic, deadline_ms=deadline_ms,
                   context_token=context_token, metadata=metadata)

    @classmethod
    def ack(cls, *, delivery_id: str, layer: str,
            message: str | None = None) -> GatewayFrame:
        return cls(type=FrameType.ACK, delivery_id=delivery_id,
                   ack_layer=layer, reason=message)

    @classmethod
    def nack(cls, *, delivery_id: str, reason: str) -> GatewayFrame:
        return cls(type=FrameType.NACK, delivery_id=delivery_id, reason=reason)

    @classmethod
    def event(cls, *, event_type: str,
              payload: dict[str, Any] | None = None) -> GatewayFrame:
        return cls(type=FrameType.EVENT, event_type=event_type, payload=payload)

    @classmethod
    def processing_complete(cls, *, message_id: str, outcome: str,
                            reason: str | None = None) -> GatewayFrame:
        """Report a terminal processing outcome for a pushed DELIVER frame."""
        payload: dict[str, Any] = {"message_id": message_id, "outcome": outcome}
        if reason:
            payload["reason"] = reason
        return cls.event(event_type="processing.complete", payload=payload)

    @classmethod
    def outbound(cls, *, origin: str, text: str,
                 context_token: str | None = None,
                 metadata: dict[str, Any] | None = None,
                 semantic_tags: list[str] | None = None,
                 in_reply_to: str | None = None) -> GatewayFrame:
        return cls(type=FrameType.OUTBOUND, origin=origin, text=text,
                   context_token=context_token,
                   metadata=dict(metadata) if metadata is not None else None,
                   semantic_tags=list(semantic_tags or []),
                   in_reply_to=in_reply_to)


class GatewayIpcError(Exception):
    """Raised on frame validation / auth failures."""


def constant_time_eq(a: str | None, b: str | None) -> bool:
    import hmac as _hmac
    if a is None or b is None:
        return a is None and b is None
    return _hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
