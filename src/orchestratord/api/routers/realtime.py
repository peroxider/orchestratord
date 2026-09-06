"""Realtime WebSocket protocol (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.4.1,
§6.4).

Implements the ``/ws`` endpoint introduced by the FastAPI split. The wire
contract carries realtime events (daemon → server → browser) and routes
``session.approve`` back the other way. This is the protocol skeleton: the
token gate is a Phase-1 stub (real auth is §5.7.4) and the topic pub/sub
backbone (§6.4) is wired when the runtime-token + reverse-heartbeat work
lands. Message shapes mirror multica ``server/internal/realtime``.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["realtime"])

# Heartbeat keeps idle connections alive so dead sockets are reaped (§6.4:
# 30s, matching multica's ``HeartbeatInterval`` default). The first interval
# is a short liveness probe so a client — and the contract test — can observe
# a ping without waiting the full 30s; subsequent pings use the 30s cadence.
_HEARTBEAT_SECONDS = 30.0
_LIVENESS_PROBE_SECONDS = 0.5


def _ws_token_valid(token: str) -> bool:
    """Phase-1 token gate.

    Real auth — cookie-based session, the ``auth_tokens`` table, and daemon
    runtime tokens (§5.7.4) — lands in Phase 2. Until then the gate rejects
    empty tokens and the reserved ``bogus`` sentinel so the 4001 close-code
    contract (§5.4.1) is exercisable; everything else is accepted.
    """
    return bool(token) and token != "bogus"


def _coerce_topics(raw: object) -> list[str]:
    """Return ``raw`` as a list of topic strings, or ``[]`` if malformed.

    ``topics`` is a list of strings on the wire (§5.4.1), but the endpoint is
    a network boundary — a non-list frame must degrade to a no-op rather than
    crash the handler with ``TypeError``.
    """
    if not isinstance(raw, list):
        return []
    return [str(t) for t in raw]


@router.websocket("/ws")
async def websocket_realtime(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    if not _ws_token_valid(token):
        await websocket.close(code=4001)
        return

    workspace_id = websocket.query_params.get("workspace_id", "")
    await websocket.accept()
    await websocket.send_json({"type": "hello", "workspace_id": workspace_id})

    topics: set[str] = set()
    first_idle = True

    while True:
        timeout = _LIVENESS_PROBE_SECONDS if first_idle else _HEARTBEAT_SECONDS
        try:
            message = await asyncio.wait_for(
                websocket.receive_json(), timeout=timeout
            )
        except TimeoutError:
            await websocket.send_json({"type": "ping", "ts": time.time()})
            first_idle = False
            continue
        except WebSocketDisconnect:
            break
        first_idle = False

        if not isinstance(message, dict):
            continue
        msg_type: str = message.get("type", "")

        if msg_type == "subscribe":
            topics.update(_coerce_topics(message.get("topics")))
            await websocket.send_json({"type": "subscribed", "topics": sorted(topics)})
        elif msg_type == "unsubscribe":
            topics.difference_update(_coerce_topics(message.get("topics")))
            await websocket.send_json(
                {"type": "unsubscribed", "topics": sorted(topics)}
            )
        elif msg_type == "session.approve":
            # Phase 2 routes this to the BackendRunner's pending-approval
            # store (§5.2.3/§5.4.1); until then acknowledge the frame so the
            # client's approve round-trip completes.
            await websocket.send_json(
                {
                    "type": "session.approve.ack",
                    "session_id": message.get("session_id", ""),
                    "tool_call_id": message.get("tool_call_id", ""),
                }
            )
