"""Peer federation frame protocol (``peer/1``).

JSONL over HTTPS with frame types: HELLO, WELCOME, INVOKE, RESULT,
EVENT, SUBSCRIBE, UNSUBSCRIBE, PING, PONG, REVOKE, GOODBYE. Frame
shape mirrors the house style of ``ipc/protocol.py`` (``gateway/1``).

Phase 1 decisions landed here (DESIGN_PEER_FEDERATION.md §11.5):

* **D18** — INVOKE/RESULT carry ``msg_id``; the receiving daemon dedups
  on ``(msg_id, orch_id)`` inside a 30s window (dispatcher, PR5).
* **D19** — INVOKE may carry ``ordering``, a per-session monotonic
  sequence number; empty means unordered.
* **D24** — GOODBYE carries ``in_flight`` so the peer can drain before
  the sender exits.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PROTOCOL_VERSION = "peer/1"


class PeerFrameType(str, Enum):
    HELLO = "hello"
    WELCOME = "welcome"
    INVOKE = "invoke"
    RESULT = "result"
    EVENT = "event"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    PING = "ping"
    PONG = "pong"
    REVOKE = "revoke"
    GOODBYE = "goodbye"


@dataclass
class PeerFrame:
    type: PeerFrameType
    frame_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    protocol_version: str = PROTOCOL_VERSION
    orch_id: str | None = None
    peer_token: str | None = None
    request_id: str | None = None
    msg_id: str | None = None
    ordering: str | None = None
    method: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | None = None
    status: int | None = None
    topic: str | None = None
    payload: dict[str, Any] | None = None
    capabilities: list[str] = field(default_factory=list)
    in_flight: int | None = None
    timestamp: float = field(default_factory=time.time)
    nonce: str | None = None
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": self.type.value,
            "frame_id": self.frame_id,
            "protocol_version": self.protocol_version,
            "timestamp": self.timestamp,
        }
        for k in (
            "orch_id", "peer_token", "request_id", "msg_id", "ordering",
            "method", "headers", "status", "topic", "payload",
            "capabilities", "in_flight", "nonce", "signature",
        ):
            v = getattr(self, k)
            if v not in (None, [], {}):
                d[k] = v
        # body={} is a legal §4.2 value and part of the MAC input (D3):
        # dropping it would make a signed empty-body frame fail verify
        # on the wire, so body ships whenever it is present.
        if self.body is not None:
            d["body"] = self.body
        return d

    def encode(self) -> bytes:
        return (json.dumps(self.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes | str) -> PeerFrame:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PeerFrame:
        if not isinstance(data, dict):
            # ValueError (not TypeError) keeps the decode contract in the
            # ValueError family: json.JSONDecodeError is a ValueError too.
            raise ValueError("frame must be a JSON object")  # noqa: TRY004
        ftype = data.get("type")
        if ftype is None:
            raise ValueError("frame missing 'type'")
        try:
            ftype_enum = PeerFrameType(ftype)
        except ValueError as exc:
            raise ValueError(f"unknown frame type {ftype!r}") from exc
        version = data.get("protocol_version", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol_version {version!r}")
        raw_ts = data.get("timestamp", time.time())
        try:
            timestamp = float(raw_ts)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"frame 'timestamp' must be a number, got {raw_ts!r}") from exc
        # A null/non-string frame_id on the wire must be rejected here:
        # frame_id feeds the MAC (D3), and a None slipping through would
        # crash sign/verify with AttributeError instead of a clean error.
        raw_fid = data.get("frame_id")
        if raw_fid is None:
            frame_id = str(uuid.uuid4())
        elif isinstance(raw_fid, str) and raw_fid:
            frame_id = raw_fid
        else:
            raise ValueError(
                f"frame 'frame_id' must be a non-empty string, got {raw_fid!r}"
            )
        return cls(
            type=ftype_enum,
            frame_id=frame_id,
            protocol_version=version,
            orch_id=data.get("orch_id"),
            peer_token=data.get("peer_token"),
            request_id=data.get("request_id"),
            msg_id=data.get("msg_id"),
            ordering=data.get("ordering"),
            method=data.get("method"),
            headers=data.get("headers") if isinstance(data.get("headers"), dict) else None,
            body=data.get("body") if isinstance(data.get("body"), dict) else None,
            status=data.get("status"),
            topic=data.get("topic"),
            payload=data.get("payload") if isinstance(data.get("payload"), dict) else None,
            capabilities=list(data.get("capabilities") or []),
            in_flight=data.get("in_flight"),
            timestamp=timestamp,
            nonce=data.get("nonce"),
            signature=data.get("signature"),
        )

    # -- convenience constructors --

    @classmethod
    def hello(cls, *, orch_id: str,
              capabilities: list[str] | None = None) -> PeerFrame:
        return cls(type=PeerFrameType.HELLO, orch_id=orch_id,
                   capabilities=list(capabilities or []))

    @classmethod
    def welcome(cls, *, orch_id: str,
                capabilities: list[str] | None = None) -> PeerFrame:
        return cls(type=PeerFrameType.WELCOME, orch_id=orch_id,
                   capabilities=list(capabilities or []))

    @classmethod
    def invoke(cls, *, orch_id: str, request_id: str, method: str,
               body: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None,
               msg_id: str | None = None,
               ordering: str | None = None) -> PeerFrame:
        # D18: every INVOKE must carry msg_id; fill one in when the
        # caller did not, so the dedup window always has a key.
        return cls(type=PeerFrameType.INVOKE, orch_id=orch_id,
                   request_id=request_id, method=method, body=body,
                   headers=headers, msg_id=msg_id or str(uuid.uuid4()),
                   ordering=ordering)

    @classmethod
    def result(cls, *, orch_id: str, request_id: str, status: int,
               body: dict[str, Any] | None = None,
               msg_id: str | None = None) -> PeerFrame:
        # RESULT echoes the INVOKE's msg_id (D18); callers that replay
        # a cached RESULT must pass the original msg_id through.
        return cls(type=PeerFrameType.RESULT, orch_id=orch_id,
                   request_id=request_id, status=status, body=body,
                   msg_id=msg_id or str(uuid.uuid4()))

    @classmethod
    def event(cls, *, orch_id: str, topic: str,
              payload: dict[str, Any] | None = None) -> PeerFrame:
        return cls(type=PeerFrameType.EVENT, orch_id=orch_id,
                   topic=topic, payload=payload)

    @classmethod
    def goodbye(cls, *, orch_id: str, in_flight: int = 0) -> PeerFrame:
        # D24: in_flight lets the peer drain before the sender exits.
        return cls(type=PeerFrameType.GOODBYE, orch_id=orch_id,
                   in_flight=in_flight)

    @classmethod
    def ping(cls, *, orch_id: str) -> PeerFrame:
        return cls(type=PeerFrameType.PING, orch_id=orch_id)

    @classmethod
    def pong(cls, *, orch_id: str) -> PeerFrame:
        return cls(type=PeerFrameType.PONG, orch_id=orch_id)
