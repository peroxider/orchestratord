"""IPC client for the orchestratord IM gateway.

UDS JSONL client for the IM gateway daemon. Connects to the gateway's
Unix domain socket, registers with ``REGISTER`` + periodic ``HEARTBEAT``,
and receives inbound messages via the ``on_deliver`` callback.

Two directions:

* request/response (register/heartbeat/control): ``_send`` writes a frame
  and awaits the matching reply, routed by a future keyed on the
  request's ``message_id`` and ``delivery_id``.
* server-pushed DELIVER frames: the background reader normalizes messages
  into a bounded queue. A separate worker invokes ``on_deliver`` in order,
  leaving the reader free to consume heartbeat and outbound replies.

``send_outbound`` carries a reply back to the gateway and waits for the
server's ACK/NACK so reliability semantics are observable by callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Self

from .models import InboundMessage
from .protocol import CHANNEL_RELOAD_TIMEOUT_SECONDS, FrameType, GatewayFrame

logger = logging.getLogger(__name__)

OnDeliverFn = Callable[[InboundMessage], Awaitable[None] | None]


class GatewayIpcClient:
    """UDS JSONL client for the IM gateway daemon.

    Connects to a Unix domain socket, registers with the gateway,
    and receives inbound messages via the ``on_deliver`` callback.
    """

    def __init__(
        self,
        sock: str,
        instance_id: str = "client",
        *,
        origin: str = "orchestrator",
        capabilities: list[str] | None = None,
        token: str | None = None,
        heartbeat_interval: float = 30.0,
        on_deliver: OnDeliverFn | None = None,
        reply_timeout: float = 5.0,
    ) -> None:
        self._sock = sock
        # Public alias so callers can compare the live socket path before
        # reconnecting.
        self.socket_path = sock
        self._instance_id = instance_id
        self._origin = origin
        self._capabilities = capabilities or []
        self._token = token
        self._heartbeat_interval = heartbeat_interval
        self._reply_timeout = reply_timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._running = False
        self._read_task: asyncio.Task[None] | None = None
        self._deliver_task: asyncio.Task[None] | None = None
        self._deliver_queue: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=128)
        self._pending: dict[str, asyncio.Future[GatewayFrame]] = {}
        self._write_lock: asyncio.Lock | None = None  # created in connect()

        # Delivery handlers may be synchronous or asynchronous.  The latter
        # is the normal orchestrator integration: it dispatches the message
        # and writes the processed ACK before accepting the next delivery.
        self.on_deliver: OnDeliverFn | None = on_deliver

    async def connect(self) -> None:
        """Open the UDS connection and start the background read loop."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._sock)
        self._write_lock = asyncio.Lock()
        self._running = True
        # A single background reader owns readline(): replies for pending
        # requests and server-pushed DELIVER frames both arrive there, so
        # register() never races a second coroutine for incoming data.
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("gateway ipc client connected: %s", self._sock)

    async def stop(self) -> None:
        """Tear down the read loop, the connection, and pending waiters."""
        self._running = False
        read_task = self._read_task
        self._read_task = None
        if read_task is not None:
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await read_task
        await self._stop_deliveries()
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionError, RuntimeError, OSError):
                pass
        self._reader = None
        self._writer = None
        # Release any waiters still blocked on a reply.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(None)  # type: ignore[arg-type]
        self._pending.clear()
        logger.debug("gateway ipc client closed")

    async def close(self) -> None:
        """Alias for stop() for callers using the close-style lifecycle."""
        await self.stop()

    async def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        while True:
            try:
                line = await reader.readline()
            except Exception:
                if self._running:
                    logger.warning("gateway ipc: read error", exc_info=True)
                break
            if not line:
                if self._running:
                    logger.info("gateway ipc: connection closed by peer")
                break
            try:
                frame = GatewayFrame.decode(line)
            except ValueError:
                logger.debug("gateway ipc: dropping undecodable frame")
                continue
            await self._dispatch_incoming(frame)
        await self._stop_deliveries()

    async def _stop_deliveries(self) -> None:
        task = self._deliver_task
        self._deliver_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        while not self._deliver_queue.empty():
            self._deliver_queue.get_nowait()
            self._deliver_queue.task_done()

    async def _deliver_loop(self) -> None:
        """Execute commands in order without holding up ACK/heartbeat reads."""
        while True:
            message = await self._deliver_queue.get()
            try:
                if self.on_deliver is not None:
                    result = self.on_deliver(message)
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                logger.exception("gateway ipc: on_deliver callback failed")
            finally:
                self._deliver_queue.task_done()

    async def _dispatch_incoming(self, frame: GatewayFrame) -> None:
        """Route an incoming frame to a pending request or ``on_deliver``."""
        # Replies (ACK/NACK) echo the original request id in
        # ``delivery_id`` (acks) or ``message_id``. Try delivery_id first
        # because ACK frames carry a fresh message_id of their own.
        fut: asyncio.Future[GatewayFrame] | None = None
        for key in (frame.delivery_id, frame.message_id):
            if key:
                fut = self._pending.pop(key, None)
                if fut is not None:
                    break
        if fut is not None:
            if not fut.done():
                fut.set_result(frame)
            return
        # Server-pushed DELIVER (inbound IM message for this opt-in host).
        if frame.type is FrameType.DELIVER and self.on_deliver is not None:
            msg = InboundMessage(
                origin=frame.origin or "",
                text=frame.text or "",
                message_id=frame.delivery_id or "",
                channel_type="gateway",
                semantic=frame.semantic,
                context_token=frame.context_token,
                semantic_tags=list(frame.semantic_tags),
                metadata=frame.metadata or {},
            )
            try:
                self._deliver_queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning("gateway ipc: inbound command queue full; rejecting delivery")
                await self.complete_processing(
                    message_id=msg.message_id, outcome="failure", reason="command queue full"
                )
                return
            if self._deliver_task is None or self._deliver_task.done():
                self._deliver_task = asyncio.create_task(self._deliver_loop())

    async def _send(
        self, frame: GatewayFrame, *, reply_timeout: float | None = None
    ) -> GatewayFrame | None:
        """Write a frame and await its reply (routed by the read loop).

        The server echoes the reply id in either ``delivery_id`` or
        ``message_id`` depending on frame kind (REGISTER acks echo
        ``message_id``; DELIVER/OUTBOUND acks echo ``delivery_id``), so the
        pending future is registered under both keys when present.

        Transport-level errors (BrokenPipeError, ConnectionResetError)
        are caught and ``None`` is returned so callers can react gracefully
        (reconnect, queue for later) instead of receiving a raw exception —
        the gateway and the orchestrator are decoupled and either may be
        stopped independently.
        """
        if self._writer is None:
            raise RuntimeError("not connected")
        keys = [k for k in (frame.message_id, frame.delivery_id) if k]
        fut: asyncio.Future[GatewayFrame] = asyncio.get_running_loop().create_future()
        for k in keys:
            self._pending[k] = fut
        # Serialize writes: asyncio StreamWriter is not safe to write from
        # multiple tasks concurrently (heartbeat vs send_outbound vs _send).
        try:
            async with self._write_lock:
                self._writer.write(frame.encode())
                await self._writer.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            for k in keys:
                self._pending.pop(k, None)
            logger.debug("gateway ipc: send failed (connection lost): %s", exc)
            return None
        if not keys:
            return None  # fire-and-forget frame (no reply expected)
        try:
            return await asyncio.wait_for(
                fut, timeout=self._reply_timeout if reply_timeout is None else reply_timeout
            )
        except TimeoutError:
            for k in keys:
                self._pending.pop(k, None)
            logger.debug("gateway ipc: reply timed out for keys=%s", keys)
            return None

    async def _write_frame_no_reply(self, frame: GatewayFrame) -> None:
        """Write a fire-and-forget frame without waiting on the read loop."""
        if self._writer is None:
            raise RuntimeError("not connected")
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        try:
            async with self._write_lock:
                self._writer.write(frame.encode())
                await self._writer.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            logger.debug("gateway ipc: send failed (connection lost): %s", exc)

    async def register(
        self,
        *,
        session_id: str | None = None,
        origin: str | None = None,
        capabilities: list[str] | None = None,
        token: str | None = None,
    ) -> GatewayFrame | None:
        """Send REGISTER frame and wait for the ACK/NACK response.

        Returns the response frame, or None on timeout / connection error.
        Omitted parameters fall back to the constructor values.
        """
        frame = GatewayFrame.register(
            session_id=session_id or self._instance_id,
            origin=origin or self._origin,
            capabilities=capabilities or self._capabilities,
            token=token if token is not None else self._token,
        )
        response = await self._send(frame)
        if response is not None and response.type is FrameType.ACK:
            self._running = True
        return response

    async def reconnect_until_registered(
        self,
        *,
        session_id: str | None = None,
        origin: str | None = None,
        capabilities: list[str] | None = None,
        token: str | None = None,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        max_attempts: int = 1,
    ) -> GatewayFrame | None:
        """(Re)connect and register with exponential backoff.

        Defaults to a single attempt because orchestrator callers wrap this
        in their own outer retry loop; raise ``max_attempts`` to retry
        here instead. Any previous connection is torn down first so repeated
        calls never leak writers or read tasks. Messages that arrived while
        the opt-in target was offline are not replayed by the gateway, so
        reconnecting only rebuilds the active binding.
        """
        sid = session_id or self._instance_id
        attempts = max(1, max_attempts)
        delay = max(0.0, base_delay)
        max_delay = max(delay, max_delay)
        for attempt in range(attempts):
            try:
                await self.stop()
                await self.connect()
                response = await self.register(
                    session_id=sid,
                    origin=origin,
                    capabilities=capabilities,
                    token=token,
                )
                if response is not None and response.ack_layer == "accepted":
                    return response
            except Exception:
                logger.debug(
                    "gateway ipc: reconnect attempt %d/%d failed",
                    attempt + 1,
                    attempts,
                    exc_info=True,
                )
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay = min(delay * 2 if delay else base_delay, max_delay)
        logger.warning(
            "gateway ipc: reconnect exhausted after %d attempts (session=%s)",
            attempts,
            sid[:16],
        )
        return None

    async def heartbeat(self) -> GatewayFrame | None:
        """Send a heartbeat and await its ACK reply."""
        return await self._send(GatewayFrame.heartbeat(session_id=self._instance_id))

    async def send_outbound(
        self,
        *,
        origin: str,
        text: str,
        context_token: str | None = None,
        metadata: dict[str, Any] | None = None,
        semantic_tags: list[str] | None = None,
        in_reply_to: str | None = None,
    ) -> GatewayFrame | None:
        """Send a reply back to the gateway for delivery to the IM origin.

        The server replies with ACK/NACK, so callers get an observable
        result while still treating delivery as best-effort at the IM
        channel layer. ``None`` means the reply timed out or the connection
        dropped (ambiguous outcome — never retry blindly).
        """
        frame = GatewayFrame.outbound(
            origin=origin,
            text=text,
            context_token=context_token,
            metadata=metadata,
            semantic_tags=semantic_tags,
            in_reply_to=in_reply_to,
        )
        response = await self._send(frame)
        if response is None:
            logger.warning("gateway ipc: OUTBOUND timed out origin=%s", origin[:24])
        elif response.type is FrameType.NACK:
            logger.warning(
                "gateway ipc: OUTBOUND rejected origin=%s reason=%s",
                origin[:24],
                response.reason or "",
            )
        else:
            logger.debug("gateway ipc: sent OUTBOUND origin=%s len=%d", origin[:24], len(text))
        return response

    async def complete_processing(
        self,
        *,
        message_id: str,
        outcome: str,
        reason: str = "",
    ) -> None:
        """Acknowledge processing of an inbound delivery.

        Sends a ``processing.complete`` EVENT frame (fire-and-forget).
        """
        await self._write_frame_no_reply(
            GatewayFrame.processing_complete(
                message_id=message_id,
                outcome=outcome,
                reason=reason or None,
            )
        )

    async def reload_channel(self, name: str) -> GatewayFrame | None:
        """Ask the gateway to reload/restart one channel."""
        return await self._send(
            GatewayFrame.event(event_type="control.reload", payload={"channel": name}),
            reply_timeout=max(self._reply_timeout, CHANNEL_RELOAD_TIMEOUT_SECONDS + 5.0),
        )

    async def unbind_origin(self, origin: str) -> GatewayFrame | None:
        """Ask the gateway to drop the binding for one origin."""
        return await self._send(
            GatewayFrame.event(event_type="control.unbind", payload={"origin": origin})
        )

    async def status(self) -> dict[str, Any] | None:
        """Query gateway status; returns the reply payload if available."""
        resp = await self._send(GatewayFrame.event(event_type="control.status"))
        if resp is not None and resp.payload is not None:
            return resp.payload
        return None

    async def unregister(self, session_id: str | None = None) -> None:
        """Best-effort unregister before closing the IPC connection."""
        if self._writer is not None:
            await self._write_frame_no_reply(
                GatewayFrame(
                    type=FrameType.UNREGISTER,
                    session_id=session_id or self._instance_id,
                )
            )
        await self.stop()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
