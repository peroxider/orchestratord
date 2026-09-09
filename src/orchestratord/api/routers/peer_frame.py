"""Inbound frame-stream endpoint for ``peer/1`` (DESIGN §6.1, PR-B2 + PR-B3).

Mounted at ``POST /peer/v1/stream`` on the main FastAPI app
(single-port design — see plan §A). The body is chunked JSONL, the
response is chunked JSONL; authentication runs once on the request
line via :func:`orchestratord.api.deps.require_peer_auth`, so per-frame
HMAC is the only replay protection on the body itself.

PR-B2 frame scope:

* HELLO → verify HMAC + nonce, allocate session, emit WELCOME.
* INVOKE → run through the existing :class:`PeerMessageDispatcher`
  (D18 dedup / D19 ordering); emit a RESULT carrying ``request_id``
  + ``msg_id`` + per-method ``status``.
* SUBSCRIBE / UNSUBSCRIBE → update per-connection topic set.
* PING → emit PONG.
* GOODBYE → drain in-flight, then close.
* REVOKE → immediate close (D15 token-revocation path).

PR-B3 method routing:

* INVOKE is dispatched through :data:`PEER_FRAME_METHOD_HANDLERS`
  (see :mod:`orchestratord.peer.method_handlers`) — 6 advertised
  capabilities have local handlers; mutating cross-process methods
  (sessions.approve, agents.message) return a 501 "cross-process
  bridge pending" envelope until PR-B4 lands. Unknown method names
  fall back to the PR-B2 stub echo (``{"status":"accepted"}``) for
  backwards compatibility with Phase 1 clients.
* **D19 ordering is inert on this path** — ``session_id=None`` is
  passed to :class:`PeerMessageDispatcher.dispatch_message` because
  the frame body does not carry a stable per-session key today.
  PR-B3 leaves the gap documented; PR-B4 may pass ``session_id``
  from the frame body when method implies one (e.g. the
  ``POST /api/sessions/{session_id}/messages`` body carries one).
  **PR-B4 wires it:** for methods in
  :data:`orchestratord.peer.method_handlers.SESSION_ID_METHODS`
  (``POST /api/sessions/{session_id}/messages`` and ``approve``) we
  extract ``session_id`` from the frame body, pre-call
  :meth:`PeerMessageDispatcher.check_ordering`, and thread the
  ``out_of_order`` flag into the handler's audit-row payload via
  ``dispatch_message(precomputed_out_of_order=...)`` — skipping the
  internal re-check to avoid double-incrementing the per-session
  chain.

**PR-B4 AC7/D22 EVENT cross-daemon relay**: SUBSCRIBE / UNSUBSCRIBE
frames now mutate the broker topic set via
:meth:`RealtimeBroker.update_topics`, and a dedicated forwarder
coroutine drains the broker iterator and emits outbound ``EVENT``
frames on the same chunked stream. AC7's ``RealtimeBackend``
abstraction (``LocalBackend`` / ``RedisBackend``) and the
``PeerEventRelay`` Redis subscriber are reused unchanged — the frame
path plugs into the same seam the WebSocket ``/ws`` route uses.

EVENT frames from the client are not consumed by the server (AC7 / D22
relay is the cross-daemon event channel; the frame stream is
request/response for now).

Reader and writer loops are decoupled by :class:`asyncio.Queue` so a
stalled peer never wedges the chunked POST (deadlock prevention — see
plan §C).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from orchestratord.api.deps import require_peer_auth
from orchestratord.api.routers.peer import _get_dispatcher
from orchestratord.peer import connections as peer_connections
from orchestratord.peer.hmac_sig import PeerAuthError, verify
from orchestratord.peer.method_handlers import PEER_FRAME_METHOD_HANDLERS
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

    PR-B3 routing: the inner ``_execute`` closure looks up ``method``
    in :data:`PEER_FRAME_METHOD_HANDLERS`. A registered method gets a
    fresh DB session (matches REST semantics — one transaction per
    handler invocation) and runs the handler; HTTPException is
    translated into a ``(__status__, __error__)`` envelope that the
    post-dispatch branch unwraps into the RESULT frame's ``status``
    field. Unknown methods fall back to the PR-B2 stub echo so
    Phase 1 clients that emit method names outside the advertised set
    still get a 200 back.

    PR-B4 D19 ordering on this path: for methods in
    :data:`SESSION_ID_METHODS` (``POST /api/sessions/{session_id}/
    messages`` and ``approve``) we extract ``session_id`` from the
    frame body and call :meth:`PeerMessageDispatcher.check_ordering`
    BEFORE :meth:`dispatch_message` to update the per-session chain.
    The flag is captured into the closure so the handler writes the
    audit row with ``out_of_order`` in the payload (parity with REST
    at ``peer.py:347``). ``precomputed_out_of_order`` skips the
    dispatcher's internal re-check — otherwise the per-session chain
    would double-increment and every subsequent message would falsely
    read as out-of-order.
    """
    from orchestratord.api.db import _get_session_factory
    from orchestratord.db.repository import Repositories
    from orchestratord.peer.method_handlers import _extract_session_id

    dispatcher = _get_dispatcher()
    method = frame.method or ""
    msg_id = frame.msg_id or ""
    request_id = frame.request_id or ""

    # PR-B4: extract session_id from body for session-bearing methods
    # and pre-compute the D19 ordering flag. See method_handlers.py
    # ``_extract_session_id`` for the method-list rationale.
    session_id = _extract_session_id(method, frame.body or {})
    out_of_order = False
    if session_id is not None:
        out_of_order = not dispatcher.check_ordering(
            peer.remote_orch_id, session_id, frame.ordering,
        )

    async def _execute() -> dict[str, Any]:
        handler = PEER_FRAME_METHOD_HANDLERS.get(method)
        if handler is None:
            # Backwards-compat: unknown method → PR-B2 stub echo.
            # Ordering / audit don't apply (no handler ran).
            return {"status": "accepted", "msg_id": msg_id, "method": method}
        # One session per handler invocation (matches REST semantics:
        # commit on clean return, rollback on error).
        factory = _get_session_factory()
        async with factory() as session_dbsession:
            repos = Repositories(session_dbsession)
            try:
                result = await handler(
                    peer_row,
                    frame.body or {},
                    repos,
                    msg_id=msg_id,
                    out_of_order=out_of_order,
                )
                if isinstance(result, dict) and "__status__" in result:
                    # Bridge-pending stub: nothing was written; rollback
                    # is a no-op but keep the symmetry with the error
                    # branches below.
                    await session_dbsession.rollback()
                    return result
                await session_dbsession.commit()
                return result
            except HTTPException as exc:
                await session_dbsession.rollback()
                return {
                    "__error__": exc.detail,
                    "__status__": exc.status_code,
                }
            except Exception as exc:  # noqa: BLE001 — surface as 500 envelope
                await session_dbsession.rollback()
                logger.exception(
                    "peer /v1/stream: handler %s crashed", method
                )
                return {
                    "__error__": f"internal error: {exc!r}",
                    "__status__": 500,
                }

    await dispatcher.dispatch_message(
        orch_id=peer.remote_orch_id,
        msg_id=msg_id,
        session_id=session_id,
        ordering=frame.ordering,
        payload=frame.body,
        execute=_execute,
        precomputed_out_of_order=out_of_order,
    )
    # D18 dedup: a re-delivered msg_id returns the cached body verbatim.
    cached = dispatcher.cached_result(peer.remote_orch_id, msg_id)
    if isinstance(cached, dict) and "__status__" in cached:
        # Error envelope from the dispatch path — unwrap into RESULT
        # frame's ``status`` + a ``{"error": ..., "method": ...}`` body
        # so the client sees a clean 4xx/5xx instead of an opaque
        # "accepted" body.
        status = int(cached.get("__status__", 500))
        error = str(cached.get("__error__", "unknown"))
        body = {"error": error, "method": method}
    else:
        body = cached if cached is not None else {"status": "accepted"}
        status = 200
    return PeerFrame.result(
        orch_id=getattr(peer_row, "orch_id", "unknown"),
        request_id=request_id,
        status=status,
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

    # PR-B4 AC7/D22 event relay: subscribe to the local RealtimeBroker
    # so events published in this process (including ``peer.*``-prefixed
    # topics that arrived via Redis from a remote daemon) flow back to
    # the peer's outbound EVENT stream. ``broker.subscribe()`` returns
    # an iterator whose ``finally`` clause unsubscribes when the
    # forwarder task exits, so explicit cleanup is unnecessary. The
    # initial topic set is empty; SUBSCRIBE frames update it in place
    # via ``broker.update_topics`` — same pattern the WebSocket
    # handler uses (``api/routers/realtime.py:128-150``).
    from orchestratord.api.realtime import get_broker

    broker = get_broker()
    broker_sub_id, broker_frame_iter = await broker.subscribe(set())
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
                            # PR-B4: hand the updated topic set to the
                            # broker so ``publish()`` starts fanning
                            # frames to the forwarder iterator.
                            await broker.update_topics(
                                broker_sub_id, set(subscriptions)
                            )
                    elif frame.type is PeerFrameType.UNSUBSCRIBE:
                        if frame.topic:
                            subscriptions.discard(frame.topic)
                            await broker.update_topics(
                                broker_sub_id, set(subscriptions)
                            )
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
                    # consumed: AC7/D22 is one-way (server → client).
        except Exception:
            logger.debug(
                "peer /v1/stream reader terminated for %s",
                inbound.remote_orch_id,
                exc_info=True,
            )

    async def writer() -> Any:
        """Yield each outbound frame as JSONL until the queue sentinel.

        Frames here include both INVOKE → RESULT replies and EVENT
        frames pushed by the broker forwarder — they share the same
        outbound queue on :class:`PeerServerConnection` so the wire
        ordering is preserved naturally.
        """
        while True:
            frame = await inbound.next_outbound()
            if frame is None:
                return
            yield frame.encode()
            await asyncio.sleep(0)

    async def event_forwarder() -> None:
        """Drain broker frames and emit them as outbound EVENT frames.

        Closes naturally when ``broker_frame_iter`` is exhausted — which
        happens when the forwarder task is cancelled (peer closed the
        stream, REVOKE, GOODBYE). The iterator's ``finally`` removes the
        broker subscription so no ghost subscriber lingers.
        """
        try:
            async for broker_frame in broker_frame_iter:
                await inbound.send_frame(
                    PeerFrame.event(
                        orch_id=getattr(peer_row, "orch_id", "unknown"),
                        topic=str(broker_frame["topic"]),
                        payload=broker_frame.get("payload"),
                    )
                )
        except Exception:
            logger.debug(
                "peer /v1/stream event forwarder terminated for %s",
                inbound.remote_orch_id,
                exc_info=True,
            )

    reader_task = asyncio.create_task(reader(), name="peer-frame-reader")
    forwarder_task = asyncio.create_task(
        event_forwarder(), name="peer-frame-event-forwarder"
    )

    async def cleanup() -> None:
        # PR-B4: cancel both tasks; the broker iterator's ``finally``
        # handles the broker-side unsubscribe when ``forwarder_task``
        # is cancelled.
        for t in (reader_task, forwarder_task):
            t.cancel()
        for t in (reader_task, forwarder_task):
            try:
                await t
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