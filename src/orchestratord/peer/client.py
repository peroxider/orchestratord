"""Peer HTTP/frame client with D23 timeouts (DESIGN §6.1, §10 PR4).

Wraps the HELLO/WELCOME state machine
(:mod:`orchestratord.peer.handshake`) with the D23 retry policy —
connect 10s + HELLO 30s, retried 3× with exponential backoff, every
failure leaving no half-state — plus request/response correlation for
INVOKE→RESULT (msg_id per D18), SUBSCRIBE emission, an EVENT stream,
and a D24 GOODBYE on close.

The frame pipe is an injected
:class:`~orchestratord.peer.handshake.FrameTransport` (opened through a
factory so each retry gets a fresh connection); the HTTPS binding
lands with the PR5/PR6 server endpoints. Agent Card discovery uses a
real ``httpx`` client when a ``base_url`` is given.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from orchestratord.peer.handshake import (
    CONNECT_TIMEOUT_SECONDS,
    HELLO_TIMEOUT_SECONDS,
    MAX_HANDSHAKE_RETRIES,
    FrameTransport,
    HandshakeError,
    HandshakeResult,
    perform_handshake,
    send_subscriptions,
)
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType

REQUEST_TIMEOUT_SECONDS = HELLO_TIMEOUT_SECONDS


class PeerClientError(Exception):
    """Client-side failure (timeout, closed transport, bad reply)."""


TransportFactory = Callable[[], Awaitable[FrameTransport]]


def parse_sse_data_line(line: str) -> PeerFrame | None:
    """Decode one SSE ``data:`` payload into a :class:`PeerFrame`.

    Returns ``None`` for comments, blank lines, and non-frame data so
    a shared SSE endpoint can interleave other payloads.
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload:
        return None
    try:
        return PeerFrame.decode(payload)
    except ValueError:
        return None


class PeerClient:
    """Outbound peer session: discovery → handshake → invoke/subscribe.

    ``transport_factory`` opens a fresh :class:`FrameTransport` per
    handshake attempt. ``http`` may be a pre-built ``httpx.AsyncClient``
    (test seam); otherwise one is created with the D23 connect timeout
    when ``base_url`` discovery is requested.
    """

    def __init__(
        self,
        *,
        orch_id: str,
        token: str,
        transport_factory: TransportFactory,
        nonce_store: NonceStore,
        base_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        capabilities: list[str] | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        hello_timeout: float = HELLO_TIMEOUT_SECONDS,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        retries: int = MAX_HANDSHAKE_RETRIES,
        backoff_base: float = 0.5,
    ) -> None:
        self._orch_id = orch_id
        self._token = token
        self._transport_factory = transport_factory
        self._nonce_store = nonce_store
        self._capabilities = capabilities
        self._connect_timeout = connect_timeout
        self._hello_timeout = hello_timeout
        self._request_timeout = request_timeout
        self._retries = retries
        self._backoff_base = backoff_base
        self._base_url = base_url
        self._owns_http = http is None
        if http is not None:
            self._http = http
        elif base_url is not None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=connect_timeout,
                    read=hello_timeout,
                    write=connect_timeout,
                )
            )
        else:
            self._http = None
        self._transport: FrameTransport | None = None
        self.card: dict[str, Any] | None = None
        self.remote_orch_id: str | None = None
        self.remote_capabilities: list[str] = []
        self._pending: dict[str, asyncio.Future[PeerFrame]] = {}
        self._events: asyncio.Queue[PeerFrame | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._topics: set[str] = set()

    # -- lifecycle --

    async def open(self) -> HandshakeResult:
        """Discover, then handshake with D23 retries (10s/30s/3×).

        Each attempt opens a fresh transport via the factory; a failed
        attempt closes it (no half-state) and backs off
        ``backoff_base * 2**n`` before the next. All attempts exhausted
        raises :class:`PeerClientError`.
        """
        if self._base_url is not None and self._http is not None:
            resp = await self._http.get(
                f"{self._base_url.rstrip('/')}/.well-known/agent.json"
            )
            resp.raise_for_status()
            self.card = resp.json()
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))
            transport = await self._transport_factory()
            try:
                result = await perform_handshake(
                    transport,
                    orch_id=self._orch_id,
                    token=self._token,
                    nonce_store=self._nonce_store,
                    capabilities=self._capabilities,
                    hello_timeout=self._hello_timeout,
                )
            except HandshakeError as exc:
                last_exc = exc
                continue
            self._transport = transport
            self.remote_orch_id = result.remote_orch_id
            self.remote_capabilities = result.remote_capabilities
            from orchestratord.peer.connections import register_client

            register_client(self)
            self._reader = asyncio.create_task(
                self._reader_loop(), name="peer-client-reader"
            )
            return result
        raise PeerClientError(
            f"handshake failed after {self._retries + 1} attempts: {last_exc}"
        ) from last_exc

    @property
    def in_flight(self) -> int:
        """Requests awaiting a RESULT (D24 drain accounting)."""
        return len(self._pending)

    async def close(self, *, in_flight: int = 0) -> None:
        """Send a D24 GOODBYE and tear the session down."""
        from orchestratord.peer.connections import unregister_client

        unregister_client(self)
        if self._transport is not None:
            goodbye = PeerFrame.goodbye(
                orch_id=self._orch_id, in_flight=in_flight
            )
            try:
                await self._transport.send_frame(goodbye)
            except Exception:  # noqa: BLE001, S110 — GOODBYE is best-effort
                pass
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PeerClientError("client closed"))
        self._pending.clear()
        await self._events.put(None)
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    # -- operations --

    async def invoke(
        self,
        method: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        ordering: str | None = None,
        request_id: str | None = None,
    ) -> PeerFrame:
        """Send an INVOKE and await the correlated RESULT (D18 msg_id).

        Matching is by ``request_id``. Raises
        :class:`PeerClientError` on the request timeout or a closed
        transport.
        """
        rid = request_id or str(uuid.uuid4())
        frame = PeerFrame.invoke(
            orch_id=self._orch_id,
            request_id=rid,
            method=method,
            body=body,
            headers=headers,
            ordering=ordering,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[PeerFrame] = loop.create_future()
        self._pending[rid] = fut
        await self._send_or_fail(frame)
        try:
            return await asyncio.wait_for(fut, timeout=self._request_timeout)
        except TimeoutError as exc:
            raise PeerClientError(
                f"RESULT for request {rid} timed out "
                f"after {self._request_timeout:g}s"
            ) from exc
        finally:
            self._pending.pop(rid, None)

    async def subscribe(self, topics: list[str] | set[str]) -> None:
        """Emit SUBSCRIBE frames and remember the active topic set."""
        transport = await self._require_transport()
        await send_subscriptions(transport, topics)
        self._topics |= set(topics)

    @property
    def topics(self) -> set[str]:
        return set(self._topics)

    async def events(self) -> AsyncIterator[PeerFrame]:
        """Yield EVENT frames as they arrive (ends on close)."""
        while True:
            frame = await self._events.get()
            if frame is None:
                return
            yield frame

    # -- internals --

    async def _send_or_fail(self, frame: PeerFrame) -> None:
        transport = await self._require_transport()
        await transport.send_frame(frame)

    async def _require_transport(self) -> FrameTransport:
        if self._transport is None:
            raise PeerClientError("client is not connected")
        return self._transport

    async def _reader_loop(self) -> None:
        transport = self._transport
        assert transport is not None
        try:
            while True:
                frame = await transport.receive_frame()
                if frame.type is PeerFrameType.RESULT:
                    key = frame.request_id
                    fut = self._pending.get(key) if key else None
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                elif frame.type is PeerFrameType.EVENT:
                    await self._events.put(frame)
                elif frame.type is PeerFrameType.GOODBYE:
                    # Remote is draining (D24): end the session cleanly.
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001, S110 — closure is surfaced below
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PeerClientError("transport closed"))
            self._pending.clear()
            await self._events.put(None)
