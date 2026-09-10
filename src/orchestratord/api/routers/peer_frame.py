"""Inbound frame endpoint for ``peer/1`` — batch-POST binding (PR-B9).

Mounted at ``POST /peer/v1/stream`` on the main FastAPI app
(single-port design — see plan §A). PR-B7 proved that bidirectional
chunked POST is unusable on the locked uvicorn/starlette pair (the
request body is cut off once the response starts — see
``docs/TRANSPORT_EVALUATION_PR_B7.md`` §3), so PR-B9 re-binds the
transport: **one HTTP request is one batch**. The client writes every
frame of the logical request to the body EOF; the server reads the
whole body (dispatching frames as they arrive) and then replays
WELCOME / RESULT / PONG in one response. A session is a sequence of
batches. Unsolicited EVENT push moved to the SSE endpoint
``GET /api/peer/peers/{orch_id}/events``; its broker subscription is
the per-peer topic registry (:mod:`orchestratord.peer.topic_registry`)
that SUBSCRIBE/UNSUBSCRIBE frames below mutate.

Per-frame contract (PR-B2/PR-B3 semantics, now within one batch):

* HELLO → verify HMAC + nonce, emit WELCOME.
* INVOKE → dispatched through :data:`PEER_FRAME_METHOD_HANDLERS`
  (see :mod:`orchestratord.peer.method_handlers`) — all six advertised
  methods are real handlers since PR-B9 (``sessions.approve`` executes
  locally on the session-owner daemon and never multi-hop forwards;
  unknown method names still get the PR-B2 stub echo). Runs through the
  :class:`PeerMessageDispatcher` (D18 dedup / D19 ordering via
  ``SESSION_ID_METHODS``); RESULT carries ``request_id`` + ``msg_id``
  + per-method ``status``.
* SUBSCRIBE / UNSUBSCRIBE → add/remove the topic in the per-peer
  registry (``peer.`` prefix only).
* PING → PONG.
* GOODBYE → final batch; the peer's registry set is cleared (the
  client re-subscribes after reconnecting).
* REVOKE → final batch + registry cleared (D15 token revocation).

PR-B5 D25 rate limiting is unchanged: one bucket acquire per INVOKE
frame, an over-limit frame answered with ``status=429`` RESULT.
PR-B8 outbound compression and the trace-id echo on RESULT are
unchanged. The server never requires HELLO to precede other frames in
a batch (no cross-frame session state is kept).

Reader and writer stay decoupled by :class:`asyncio.Queue`, but run
in **sequential phases**: the body is read to EOF (frames dispatched
into the outbound queue as they arrive), and only then does the
response replay the queue. Reading the body while streaming the
response is unsound on spec_version<2.4 ASGI servers — starlette's
StreamingResponse runs a concurrent disconnect listener on the same
receive channel — which is the true root cause of the PR-B7 transport
failure. Inlining the read also means a stalled peer cannot wedge
anything: a batch is bounded by the request body, and the response is
a pure replay.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from orchestratord.api.deps import peer_rate_bucket, require_peer_auth
from orchestratord.api.routers.peer import _get_dispatcher
from orchestratord.peer import connections as peer_connections
from orchestratord.peer import topic_registry
from orchestratord.peer.hmac_sig import PeerAuthError, sign, verify
from orchestratord.peer.method_handlers import PEER_FRAME_METHOD_HANDLERS
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import (
    PeerFrame,
    PeerFrameType,
    frame_compress_min_bytes,
)
from orchestratord.peer.server_connection import PeerServerConnection
from orchestratord.peer.trace import (
    TRACE_HEADER,
    new_trace_id,
    reset_current_trace_id,
    set_current_trace_id,
    trace_id_from_headers,
)

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
    """Batch-POST peer/1 frame transport (PR-B9; supersedes PR-B2 chunked).

    The body is newline-delimited JSONL :class:`PeerFrame` objects; the
    response replays the frames the server emits for the batch, then
    ends when the body EOF reaches the reader. Authentication runs once
    on the request line via :func:`require_peer_auth` (bearer +
    ``X-Peer-Orchestrator-Id``), so subsequent frames do not need to
    re-authenticate.

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

    # PR-B9: no broker subscription is opened here any more. EVENT push
    # is served by the SSE endpoint (``GET /api/peer/peers/{orch_id}/
    # events``), which reads the per-peer topic registry that the
    # SUBSCRIBE/UNSUBSCRIBE branches below mutate.

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
                        # D15: the per-peer token is symmetric — the
                        # WELCOME is HMAC-signed with it so the client's
                        # handshake can verify we hold the same key.
                        sign(welcome, token)
                        await inbound.send_frame(welcome)
                    elif frame.type is PeerFrameType.INVOKE:
                        # PR-B5/D25: the stream path authenticates once on
                        # the request line, so ``require_peer_auth``'s
                        # path-based check never fires here. Acquire from
                        # the same shared bucket per INVOKE frame; an
                        # over-limit frame gets a 429 RESULT and the
                        # connection stays up (REST parity: 429 +
                        # Retry-After).
                        retry_after = peer_rate_bucket().try_acquire(
                            str(peer_row.id)
                        )
                        if retry_after is not None:
                            await inbound.send_frame(
                                PeerFrame.result(
                                    orch_id=peer_row.orch_id,
                                    request_id=frame.request_id or "",
                                    status=429,
                                    body={
                                        "error": "peer rate limit exceeded",
                                        "retry_after": math.ceil(retry_after),
                                    },
                                    msg_id=frame.msg_id or "",
                                )
                            )
                            continue
                        inbound.entered()
                        # Trace correlation (PR 收尾): bind the INVOKE's
                        # x-trace-id (or a fresh one) for this dispatch
                        # context and echo it back on the RESULT.
                        trace_id = (
                            trace_id_from_headers(frame.headers)
                            or new_trace_id()
                        )
                        trace_token = set_current_trace_id(trace_id)
                        try:
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
                            result.headers = {TRACE_HEADER: trace_id}
                            try:
                                await inbound.send_frame(result)
                            finally:
                                inbound.released()
                        finally:
                            reset_current_trace_id(trace_token)
                    elif frame.type is PeerFrameType.SUBSCRIBE:
                        if frame.topic:
                            # PR-B9: the SSE endpoint is the broker
                            # consumer now — record the topic in the
                            # per-peer registry for it to pick up.
                            topic_registry.add_peer_topic(
                                peer_row.orch_id, frame.topic
                            )
                    elif frame.type is PeerFrameType.UNSUBSCRIBE:
                        if frame.topic:
                            topic_registry.remove_peer_topic(
                                peer_row.orch_id, frame.topic
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
                        # PR-B9: the session ends — drop the peer's SSE
                        # topic set (the client re-subscribes on the
                        # next session's handshake).
                        topic_registry.clear_peer_topics(peer_row.orch_id)
                        return
                    elif frame.type is PeerFrameType.REVOKE:
                        logger.warning(
                            "peer /v1/stream: REVOKE from %s — closing",
                            inbound.remote_orch_id,
                        )
                        topic_registry.clear_peer_topics(peer_row.orch_id)
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
        """Yield each queued frame as JSONL until the queue sentinel.

        Under the batch-POST binding the queue only ever holds
        WELCOME / RESULT / PONG replies to frames in this batch —
        unsolicited EVENT push left for the SSE endpoint (PR-B9).
        """
        while True:
            frame = await inbound.next_outbound()
            if frame is None:
                return
            # PR-B8: compress large body/payload on the way out
            # (threshold read once per stream).
            yield frame.encode(min_bytes=frame_compress_min_bytes())
            await asyncio.sleep(0)

    # PR-B9: the batch body is consumed to EOF *before* the response
    # starts. Reading the body while streaming the response is broken
    # on every spec_version<2.4 ASGI server — starlette's
    # StreamingResponse runs a concurrent ``listen_for_disconnect``
    # task against the same receive channel, so the two consumers race
    # for the body chunks (the actual PR-B7 root cause, not just
    # "body cut off after response start"). The reader therefore runs
    # inline here; the response below is a pure replay of the outbound
    # queue, ended by the sentinel ``aclose`` puts.
    await reader()
    await inbound.aclose()

    async def cleanup() -> None:
        await inbound.aclose()
        peer_connections.unregister_inbound(inbound)

    response = StreamingResponse(
        writer(), media_type="application/x-ndjson"
    )
    response.background = BackgroundTask(cleanup)
    return response


__all__ = ["router", "peer_frame_stream", "reset_nonce_store"]