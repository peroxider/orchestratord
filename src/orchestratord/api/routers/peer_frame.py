"""Inbound frame-stream endpoint for ``peer/1`` (DESIGN §6.1, PR-B2).

Mounted at ``POST /peer/v1/stream`` on the main FastAPI app
(single-port design — see plan §A). The body is chunked JSONL, the
response is chunked JSONL; authentication runs once on the request
line via :func:`orchestratord.api.deps.require_peer_auth`, so per-frame
HMAC is the only replay protection on the body itself.

PR-B2 frame scope:

* HELLO → verify HMAC + nonce, allocate session, emit WELCOME.
* INVOKE → run through the existing :class:`PeerMessageDispatcher`
  (D18 dedup / D19 ordering) with a stub ``execute``; emit a RESULT
  carrying ``request_id`` + ``msg_id``. Full method-to-handler
  routing (sessions.post, agents.message, …) lives in a follow-up —
  PR-B2 is the wire, not the entire method surface.
* SUBSCRIBE / UNSUBSCRIBE → update per-connection topic set.
* PING → emit PONG.
* GOODBYE → drain in-flight, then close.
* REVOKE → immediate close (D15 token-revocation path).

EVENT frames from the client are not consumed by the server in PR-B2
(AC7 / D22 relay is the cross-daemon event channel; the frame stream
is request/response for now).

Reader and writer loops are decoupled by :class:`asyncio.Queue` so a
stalled peer never wedges the chunked POST (deadlock prevention — see
plan §C).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from orchestratord.api.deps import require_peer_auth
from orchestratord.api.routers.peer import _get_dispatcher
from orchestratord.peer import connections as peer_connections
from orchestratord.peer.hmac_sig import PeerAuthError, verify
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType
from orchestratord.peer.server_connection import PeerServerConnection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["peer-frame"])

_NONCE_STORE: NonceStore | None = None


def _nonce_store() -> NonceStore:
    """Process-local R2 nonce replay guard (lazy, env-overridable path)."""
    global _NONCE_STORE
    if _NONCE_STORE is None:
        _NONCE_STORE = NonceStore(
            os.environ.get(
                "ORCHESTRATORD_PEER_NONCE_PATH",
                "/tmp/orchestratord_peer_nonces.db",
            )
        )
    return _NONCE_STORE


def reset_nonce_store() -> None:
    """Test seam: drop the cached nonce store so the next request rebuilds."""
    global _NONCE_STORE
    _NONCE_STORE = None


async def _dispatch_invoke_frame(
    frame: PeerFrame, peer: PeerServerConnection, peer_row: Any
) -> PeerFrame:
    """Run an INVOKE through the dispatcher and return its RESULT frame.

    PR-B2 ``execute`` is a stub: it echoes the message back with an
    ``accepted`` status so the wire round-trip is testable without the
    full method router. The dispatcher handles D18 dedup and D19
    ordering on top; a follow-up replaces the stub with the real
    per-method dispatch.
    """
    dispatcher = _get_dispatcher()
    method = frame.method or ""
    msg_id = frame.msg_id or ""
    request_id = frame.request_id or ""

    async def _execute() -> dict[str, Any]:
        # Real method handlers land in the follow-up PR; the stub
        # preserves wire semantics so the frame transport is end-to-end
        # testable.
        return {"status": "accepted", "msg_id": msg_id, "method": method}

    await dispatcher.dispatch_message(
        orch_id=peer.remote_orch_id,
        msg_id=msg_id,
        session_id=None,
        ordering=frame.ordering,
        payload=frame.body,
        execute=_execute,
    )
    # D18 dedup: a re-delivered msg_id returns the cached body verbatim.
    cached = dispatcher.cached_result(peer.remote_orch_id, msg_id)
    body = cached if cached is not None else {"status": "accepted"}
    return PeerFrame.result(
        orch_id=getattr(peer_row, "orch_id", "unknown"),
        request_id=request_id,
        status=200,
        body=body,
        msg_id=msg_id,
    )


@router.post(
    "/peer/v1/stream",
    dependencies=[Depends(require_peer_auth)],
)
async def peer_frame_stream(
    request: Request,
    peer_row: Any = Depends(require_peer_auth),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """Long-lived chunked POST peer/1 frame transport (PR-B2 / DESIGN §4.1).

    The body is newline-delimited JSONL :class:`PeerFrame` objects; the
    response is the same. Authentication runs once on the request line
    via :func:`require_peer_auth` (bearer + ``X-Peer-Orchestrator-Id``),
    so subsequent frames do not need to re-authenticate.

    Frame types handled: HELLO, INVOKE, SUBSCRIBE, UNSUBSCRIBE, PING,
    GOODBYE, REVOKE. See module docstring for the per-frame contract.
    """
    inbound = PeerServerConnection(peer=peer_row)
    peer_connections.register_inbound(inbound)
    nonce_store = _nonce_store()
    # ``require_peer_auth`` already validated the bearer token; we
    # re-read the header so HMAC frames (D3 second-layer auth, ADR-001)
    # can be verified with the same symmetric key.
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("Bearer ") :].strip()
    subscriptions: set[str] = set()

    async def reader() -> None:
        """Decode + dispatch every inbound frame; exits on GOODBYE / REVOKE."""
        try:
            async for raw in request.stream():
                for line in raw.splitlines():
                    if not line.strip():
                        continue
                    try:
                        frame = PeerFrame.decode(line)
                    except ValueError as exc:
                        logger.warning(
                            "peer /v1/stream: bad frame from %s: %s",
                            inbound.remote_orch_id,
                            exc,
                        )
                        return
                    if frame.type is PeerFrameType.HELLO:
                        if not token:
                            logger.warning(
                                "peer /v1/stream: HELLO without recoverable "
                                "token from %s — closing",
                                inbound.remote_orch_id,
                            )
                            return
                        try:
                            await verify(frame, token, nonce_store)
                        except PeerAuthError as exc:
                            logger.warning(
                                "peer /v1/stream: HELLO verify failed "
                                "for %s: %s",
                                inbound.remote_orch_id,
                                exc,
                            )
                            return
                        welcome = PeerFrame.welcome(
                            orch_id=peer_row.orch_id,
                            capabilities=list(
                                getattr(peer_row, "capabilities", []) or []
                            ),
                        )
                        await inbound.send_frame(welcome)
                    elif frame.type is PeerFrameType.INVOKE:
                        inbound.entered()
                        try:
                            result = await _dispatch_invoke_frame(
                                frame, inbound, peer_row
                            )
                        except Exception:
                            logger.exception(
                                "peer /v1/stream: INVOKE dispatch failed "
                                "for %s",
                                inbound.remote_orch_id,
                            )
                            inbound.released()
                            return
                        try:
                            await inbound.send_frame(result)
                        finally:
                            inbound.released()
                    elif frame.type is PeerFrameType.SUBSCRIBE:
                        if frame.topic:
                            subscriptions.add(frame.topic)
                    elif frame.type is PeerFrameType.UNSUBSCRIBE:
                        if frame.topic:
                            subscriptions.discard(frame.topic)
                    elif frame.type is PeerFrameType.PING:
                        await inbound.send_frame(
                            PeerFrame.pong(orch_id=peer_row.orch_id)
                        )
                    elif frame.type is PeerFrameType.GOODBYE:
                        logger.info(
                            "peer /v1/stream: GOODBYE from %s "
                            "(in_flight=%d)",
                            inbound.remote_orch_id,
                            inbound.in_flight,
                        )
                        return
                    elif frame.type is PeerFrameType.REVOKE:
                        logger.warning(
                            "peer /v1/stream: REVOKE from %s — closing",
                            inbound.remote_orch_id,
                        )
                        return
                    # EVENT frames from the client are intentionally not
                    # consumed in PR-B2 (AC7/D22 relay is the event path).
        except Exception:
            logger.debug(
                "peer /v1/stream reader terminated for %s",
                inbound.remote_orch_id,
                exc_info=True,
            )

    async def writer() -> Any:
        """Yield each outbound frame as JSONL until the queue sentinel."""
        while True:
            frame = await inbound.next_outbound()
            if frame is None:
                return
            yield frame.encode()
            await asyncio.sleep(0)

    reader_task = asyncio.create_task(reader(), name="peer-frame-reader")

    async def cleanup() -> None:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        await inbound.aclose()
        peer_connections.unregister_inbound(inbound)

    response = StreamingResponse(
        writer(), media_type="application/x-ndjson"
    )
    response.background = BackgroundTask(cleanup)
    return response


__all__ = ["router", "peer_frame_stream", "reset_nonce_store"]