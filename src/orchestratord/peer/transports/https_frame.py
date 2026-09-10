"""HTTPS chunked-JSONL frame transport (DESIGN §6.1, PR-B2).

The :class:`FrameTransport` Protocol from
:mod:`orchestratord.peer.handshake` is wire-agnostic; this module binds
it to the wire format the ``POST /peer/v1/stream`` server endpoint
expects — bidirectional chunked HTTP carrying newline-delimited JSONL
``PeerFrame`` objects.

Wire format (MCP Streamable HTTP style):

* request  body — JSONL, ``Content-Type: application/x-ndjson``,
  ``Transfer-Encoding: chunked``,
* response body — JSONL of the same shape,
* authentication is performed once on the request line by
  ``require_peer_auth`` (bearer + ``X-Peer-Orchestrator-Id``); per-frame
  HMAC rides the JSONL payload, so the TLS layer plus the upstream
  bearer token gate are sufficient.

Both directions are decoupled by ``asyncio.Queue``s so the client never
blocks waiting for the server to drain its outbound buffer (chunked POST
deadlock prevention — see plan §C).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from orchestratord.peer.handshake import CONNECT_TIMEOUT_SECONDS, FrameTransport
from orchestratord.peer.protocol import PeerFrame

logger = logging.getLogger(__name__)

# Cap the per-connection outbound queue (PR-B2). The server reads at
# line-rate; a slow peer / dead socket back-pressures here rather than
# letting the caller pile up unbounded frames.
_OUTBOUND_QUEUE_MAXSIZE = 256


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
    """Client-side :class:`FrameTransport` over HTTPS chunked JSONL.

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
        outbound_queue_size: int = _OUTBOUND_QUEUE_MAXSIZE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._orch_id = orch_id
        self._token = token
        self._connect_timeout = connect_timeout
        self._outbound: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=outbound_queue_size
        )
        self._inbound: asyncio.Queue[PeerFrame] = asyncio.Queue()
        self._owns_client = client is None
        self._client: httpx.AsyncClient | None = client
        self._response: httpx.Response | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connected = False

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
        """Open a fresh HTTPS frame transport (Phase B PR-B2 binding).

        Called by :class:`~orchestratord.peer.client.PeerClient` once
        the remote Agent Card has advertised a ``frame`` transport and
        ``transport="auto"`` was selected. The connection is fully
        async-iterable on both ends before this returns; the caller can
        drive HELLO immediately.
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
                    read=None,  # long-lived stream — no read deadline
                    write=self._connect_timeout,
                ),
                # PR-B6: self-signed peer CAs / dev insecure mode.
                verify=peer_tls_verify(),
            )

        async def body_gen() -> Any:
            while True:
                chunk = await self._outbound.get()
                if chunk is None:
                    return
                yield chunk

        req = self._client.build_request(
            "POST",
            self._url,
            content=body_gen(),
            headers={
                "Content-Type": "application/x-ndjson",
                "Authorization": f"Bearer {self._token}",
                "X-Peer-Orchestrator-Id": self._orch_id,
            },
        )
        # ``send(req, stream=True)`` keeps the response body open until
        # the caller iterates — required for bidirectional chunked POST.
        self._response = await self._client.send(req, stream=True)
        self._connected = True
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="peer-frame-reader"
        )

    # -- FrameTransport Protocol --

    async def send_frame(self, frame: PeerFrame) -> None:
        """Encode one frame as JSONL + newline and enqueue for the body."""
        if not self._connected:
            raise RuntimeError("transport not connected")
        await self._outbound.put(frame.encode())

    async def receive_frame(self) -> PeerFrame:
        """Block until the server pushes the next decoded frame."""
        if not self._connected:
            raise RuntimeError("transport not connected")
        return await self._inbound.get()

    async def close(self) -> None:
        """Tear the transport down. Idempotent (D23: no half-state)."""
        if not self._connected:
            return
        self._connected = False
        # Sentinel the body generator so the server gets a clean EOF
        # boundary; cancel the reader task and close the response.
        try:
            self._outbound.put_nowait(None)
        except asyncio.QueueFull:
            await self._outbound.put(None)
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._response is not None:
            try:
                await self._response.aclose()
            except Exception:
                pass
            self._response = None
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # -- reader loop --

    async def _reader_loop(self) -> None:
        """Drain the server's JSONL stream into the inbound queue."""
        assert self._response is not None
        try:
            async for line in self._response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    frame = PeerFrame.decode(line)
                except ValueError as exc:
                    # A malformed server frame ends the reader — the
                    # outer PeerClient._reader_loop will close the
                    # session on the resulting CancelledError/Exception.
                    logger.warning(
                        "peer frame reader: bad frame from %s: %s",
                        self._url,
                        exc,
                    )
                    raise
                await self._inbound.put(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The outer reader in PeerClient treats any exception as
            # "transport closed" and tears down pending futures; this
            # loop only translates wire errors into that signal.
            logger.debug(
                "peer frame reader loop terminated for %s", self._url,
                exc_info=True,
            )