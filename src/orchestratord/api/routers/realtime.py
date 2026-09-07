"""Realtime WebSocket protocol (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.1,
§5.4.1, §6.4).

Implements the ``/ws`` endpoint introduced by the FastAPI split. The wire
contract carries realtime events (daemon → server → browser) and routes
``session.approve`` back the other way. The topic pub/sub backbone (§5.1)
is the process-wide :class:`orchestratord.api.realtime.RealtimeBroker`
singleton — the WebSocket handler subscribes to a topic set, mutates that
set via :meth:`update_topics` on subscribe/unsubscribe frames, and runs a
concurrent broadcast loop that pulls frames from the broker's async
iterator. Message shapes mirror multica ``server/internal/realtime``.

This is still a Phase-1 protocol skeleton: ``session.approve`` acknowledges
but does not yet route to the BackendRunner (that lands in Phase A.2 §5.2.3).
The token gate validates the query-param plaintext against the
``auth_tokens`` table (only its SHA-256 is persisted; §5.4.1's 4001
close-code contract covers rejects).
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from orchestratord.api.db import get_repositories
from orchestratord.api.deps import _is_expired
from orchestratord.api.realtime import get_broker
from orchestratord.db.repository import Repositories
from orchestratord.domain.auth_token import hash_api_token

router = APIRouter(tags=["realtime"])

# Heartbeat keeps idle connections alive so dead sockets are reaped (§6.4:
# 30s, matching multica's ``HeartbeatInterval`` default). The first interval
# is a short liveness probe so a client — and the contract test — can observe
# a ping without waiting the full 30s; subsequent pings use the 30s cadence.
_HEARTBEAT_SECONDS = 30.0
_LIVENESS_PROBE_SECONDS = 0.5


async def _ws_token_valid(repos: Repositories, token: str) -> bool:
    """Token gate: the plaintext must hash to a persisted, unexpired row.

    Mirrors the REST ``require_auth`` contract — the query-param plaintext's
    SHA-256 must match an ``auth_tokens`` row that has not expired.
    """
    if not token:
        return False
    record = await repos.auth_tokens.by_token_hash(hash_api_token(token))
    return record is not None and not _is_expired(record.expires_at)


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
async def websocket_realtime(
    websocket: WebSocket,
    repos: Repositories = Depends(get_repositories),
) -> None:
    token = websocket.query_params.get("token", "")
    if not await _ws_token_valid(repos, token):
        await websocket.close(code=4001)
        return

    workspace_id = websocket.query_params.get("workspace_id", "")
    await websocket.accept()
    await websocket.send_json({"type": "hello", "workspace_id": workspace_id})

    broker = get_broker()
    topics: set[str] = set()
    sub_id, frame_iter = await broker.subscribe(topics)
    # Mutable flag shared by the broadcast loop so the 0.5s liveness probe
    # fires only once; subsequent idle intervals use the 30s heartbeat.
    state = {"first_idle": True}

    async def _receive_loop() -> None:
        """Process client frames: subscribe/unsubscribe/session.approve.

        Runs until the socket disconnects, at which point ``receive_json``
        raises :class:`WebSocketDisconnect` and propagates to ``gather``,
        cancelling the broadcast loop in the same ``gather`` call.
        """
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            msg_type: str = message.get("type", "")
            if msg_type == "subscribe":
                topics.update(_coerce_topics(message.get("topics")))
                await broker.update_topics(sub_id, topics)
                await websocket.send_json(
                    {"type": "subscribed", "topics": sorted(topics)}
                )
            elif msg_type == "unsubscribe":
                topics.difference_update(_coerce_topics(message.get("topics")))
                await broker.update_topics(sub_id, topics)
                await websocket.send_json(
                    {"type": "unsubscribed", "topics": sorted(topics)}
                )
            elif msg_type == "session.approve":
                # Phase A.2 (§5.2.3) routes this to the BackendRunner's
                # pending-approval store; until then acknowledge the frame so
                # the client's approve round-trip completes.
                await websocket.send_json(
                    {
                        "type": "session.approve.ack",
                        "session_id": message.get("session_id", ""),
                        "tool_call_id": message.get("tool_call_id", ""),
                    }
                )

    async def _broadcast_loop() -> None:
        """Pull frames from the broker iterator and push them as ``event``s.

        Also emits the heartbeat ping: 0.5s after the last activity (or
        connect), then 30s thereafter. The first ping is the liveness probe
        that the contract test asserts; later pings are the idle keepalive.
        """
        while True:
            timeout = (
                _LIVENESS_PROBE_SECONDS if state["first_idle"] else _HEARTBEAT_SECONDS
            )
            try:
                frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=timeout)
            except TimeoutError:
                await websocket.send_json({"type": "ping", "ts": time.time()})
                state["first_idle"] = False
                continue
            except StopAsyncIteration:
                # Broker removed our subscription (e.g. test teardown);
                # exit cleanly so ``gather`` unblocks.
                return
            state["first_idle"] = False
            await websocket.send_json({"type": "event", **frame})

    try:
        await asyncio.gather(_receive_loop(), _broadcast_loop())
    except WebSocketDisconnect:
        pass
    finally:
        # Idempotent — the broker's iterator ``finally`` also calls
        # ``unsubscribe`` when this task ends, so this is a belt-and-braces
        # cleanup that survives a cancel-mid-iter race.
        await broker.unsubscribe(sub_id)

