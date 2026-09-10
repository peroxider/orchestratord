"""HTTPS batch-POST JSONL frame transport (DESIGN §6.1, PR-B9).

The :class:`FrameTransport` Protocol from
:mod:`orchestratord.peer.handshake` is wire-agnostic; this module binds
it to the wire format the ``POST /peer/v1/stream`` server endpoint
expects — **one HTTP request is one batch** (PR-B9; the PR-B2
bidirectional chunked binding was abandoned — the request body cannot
be read while the response streams, see
``docs/TRANSPORT_EVALUATION_PR_B7.md`` §3/§5).

Wire format:

* request  body — JSONL, ``Content-Type: application/x-ndjson``; the
  client buffers frames sent without ``end_of_batch`` and flushes the
  whole batch as one POST when a frame arrives with
  ``end_of_batch=True``,
* response body — JSONL replay of the server's WELCOME / RESULT / PONG
  replies for that batch,
* authentication is performed once per request line by
  ``require_peer_auth`` (bearer + ``X-Peer-Orchestrator-Id``); per-frame
  HMAC rides the JSONL payload.
* unsolicited EVENT push rides the SSE endpoint
  (``GET /api/peer/peers/{orch_id}/events``), consumed by
  :class:`~orchestratord.peer.client.PeerClient`, not by this
  transport.

Concurrent logical requests (e.g. concurrent ``PeerClient.invoke``)
flush as concurrent POSTs sharing one ``httpx.AsyncClient`` connection
pool, so the PR-B3 pipelined bench scenario keeps working. A failed
batch raises through :meth:`receive_frame` (as a ``RuntimeError``) so
the outer PeerClient reader tears the session down — the PR-B2
reader-exception semantics, unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from orchestratord.peer.handshake import CONNECT_TIMEOUT_SECONDS, FrameTransport
from orchestratord.peer.protocol import PeerFrame, frame_compress_min_bytes

logger = logging.getLogger(__name__)

# Sentinel dropped into the inbound queue when a batch POST fails; the
# next receive_frame raises on it (session-teardown signal, PR-B2
# semantics).
_BATCH_FAILED: Any = object()


def peer_tls_verify() -> str | bool:
    """TLS trust configuration for outbound peer connections (PR-B6).

    Resolution order:

    1. ``ORCHESTRATORD_PEER_TLS_INSECURE=1`` → ``False`` (verification
       disabled — development/diagnostics ONLY; a loud warning is
       logged every call so it can never be enabled silently).
    2. ``ORCHESTRATORD_PEER_TLS_CA=/path/ca.pem`` → trust that CA
       bundle (the self-signed mini-CA produced by
       ``scripts/gen_peer_tls_certs.sh``).
    3. otherwise → ``True`` (system trust store; a publicly-trusted
       cert or a CA installed system-wide works with no extra config).

    Read per client construction so two-daemon integration runs can
    flip it per process via env.
    """
    if os.environ.get("ORCHESTRATORD_PEER_TLS_INSECURE", "") == "1":
        logger.warning(
            "ORCHESTRATORD_PEER_TLS_INSECURE=1 — peer TLS certificate "
            "verification DISABLED (development/diagnostics only)"
        )
        return False
    ca_path = os.environ.get("ORCHESTRATORD_PEER_TLS_CA", "").strip()
    if ca_path:
        return ca_path
    return True


class HttpsFrameTransport:
    """Client-side :class:`FrameTransport` over HTTPS batch-POST JSONL.

    Constructed by the factory selector in
    :mod:`orchestratord.peer.transports` once ``PeerClient.open`` has
    discovered a frame transport entry in the remote Agent Card. One
    instance per handshake — a failed handshake discards it (D23, no
    half-state); a successful handshake hands the transport to
    :class:`~orchestratord.peer.client.PeerClient` for the session's
    lifetime.
    """

    def __init__(
        self,
        *,
        url: str,
        orch_id: str,
        token: str,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._orch_id = orch_id
        self._token = token
        self._connect_timeout = connect_timeout
        self._inbound: asyncio.Queue[Any] = asyncio.Queue()
        self._owns_client = client is None
        self._client: httpx.AsyncClient | None = client
        self._connected = False
        # PR-B9: frames accumulate here until a call passes
        # ``end_of_batch``; the buffered batch then ships as one POST.
        # A trailing unterminated buffer is dropped on close.
        self._buffer: list[bytes] = []
        self._inflight: set[asyncio.Task[None]] = set()

    # -- FrameTransport factory --

    @classmethod
    async def connect(
        cls,
        *,
        url: str,
        orch_id: str,
        token: str,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> "HttpsFrameTransport":
        """Open a fresh HTTPS frame transport (PR-B9 batch binding).

        Called by :class:`~orchestratord.peer.client.PeerClient` once
        the remote Agent Card has advertised a ``frame`` transport and
        ``transport="auto"`` was selected. No HTTP request is opened
        here — batches ship lazily on ``end_of_batch`` — the client is
        only constructed so the caller can drive HELLO immediately.
        """
        transport = cls(
            url=url,
            orch_id=orch_id,
            token=token,
            connect_timeout=connect_timeout,
        )
        await transport._open()
        return transport

    async def _open(self) -> None:
        if self._connected:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self._connect_timeout,
                    read=None,  # a batch reply may take as long as its INVOKE
                    write=self._connect_timeout,
                    pool=self._connect_timeout,
                ),
                # PR-B6: self-signed peer CAs / dev insecure mode.
                verify=peer_tls_verify(),
            )
        self._connected = True

    # -- FrameTransport Protocol --

    async def send_frame(
        self, frame: PeerFrame, *, end_of_batch: bool = False
    ) -> None:
        """Buffer one frame; flush the batch when *end_of_batch* is set.

        PR-B8: large body/payload values ship gzip+base64-compressed
        (threshold from ``ORCHESTRATORD_PEER_FRAME_COMPRESS_BYTES``);
        the server restores them transparently on decode.
        """
        if not self._connected:
            raise RuntimeError("transport not connected")
        self._buffer.append(
            frame.encode(min_bytes=frame_compress_min_bytes())
        )
        if end_of_batch:
            payload = b"".join(self._buffer)
            self._buffer.clear()
            task = asyncio.create_task(self._run_batch(payload))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def receive_frame(self) -> PeerFrame:
        """Block until the next decoded frame from any batch response.

        A failed batch surfaces here as ``RuntimeError`` so the outer
        PeerClient reader treats it as transport-closed and tears down
        pending futures.
        """
        if not self._connected:
            raise RuntimeError("transport not connected")
        item = await self._inbound.get()
        if item is _BATCH_FAILED:
            raise RuntimeError("peer frame batch request failed")
        return item

    async def close(self) -> None:
        """Tear the transport down. Idempotent (D23: no half-state)."""
        if not self._connected:
            return
        self._connected = False
        # In-flight batches get a grace window to complete — close()
        # itself just flushed the D24 GOODBYE batch, and cancelling it
        # would drop the goodbye on the wire. Anything still pending
        # after the window is torn down.
        if self._inflight:
            _done, pending = await asyncio.wait(
                set(self._inflight), timeout=5.0
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._buffer.clear()
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # -- batch runner --

    async def _run_batch(self, payload: bytes) -> None:
        """POST one batch and feed the replayed frames into the inbound queue.

        Runs as its own task per batch: concurrent logical requests are
        concurrent POSTs over the shared connection pool. Any failure
        (connect, write, non-stream response, reader error) drops the
        ``_BATCH_FAILED`` sentinel so the next ``receive_frame`` raises
        and the session tears down.
        """
        client = self._client
        assert client is not None
        response: httpx.Response | None = None
        try:
            req = client.build_request(
                "POST",
                self._url,
                content=payload,
                headers={
                    "Content-Type": "application/x-ndjson",
                    "Authorization": f"Bearer {self._token}",
                    "X-Peer-Orchestrator-Id": self._orch_id,
                },
            )
            response = await client.send(req, stream=True)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    frame = PeerFrame.decode(line)
                except ValueError as exc:
                    logger.warning(
                        "peer frame batch: bad frame from %s: %s",
                        self._url,
                        exc,
                    )
                    raise
                await self._inbound.put(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "peer frame batch failed for %s", self._url, exc_info=True
            )
            await self._inbound.put(_BATCH_FAILED)
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass