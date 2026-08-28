"""IPC client for the orchestratord IM gateway.

Self-contained UDS JSONL client for the IM gateway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .protocol import FrameType, GatewayFrame, GatewayIpcError
from .models import InboundMessage

logger = logging.getLogger(__name__)


class GatewayIpcClient:
    """UDS JSONL client for the IM gateway daemon.

    Connects to a Unix domain socket, registers with the gateway,
    and receives inbound messages via the ``on_deliver`` callback.
    """

    def __init__(
        self,
        sock: str,
        instance_id: str,
        *,
        origin: str = "orchestrator",
        capabilities: list[str] | None = None,
        token: str | None = None,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self._sock = sock
        self._instance_id = instance_id
        self._origin = origin
        self._capabilities = capabilities or []
        self._token = token
        self._heartbeat_interval = heartbeat_interval

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._read_task: asyncio.Task | None = None

        self.on_deliver: Callable[[InboundMessage], None] | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(self._sock)

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
        """
        if self._writer is None:
            raise GatewayIpcError("Not connected — call connect() first")
        sid = session_id or self._instance_id
        reg_frame = GatewayFrame.register(
            session_id=sid,
            origin=origin or self._origin,
            capabilities=capabilities or self._capabilities,
            token=token if token is not None else self._token,
        )
        self._writer.write(reg_frame.encode())
        await self._writer.drain()
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._read_task = asyncio.create_task(self._read_loop())
        # Read the first response line — should be ACK or NACK.
        try:
            line = await asyncio.wait_for(self._reader.readline(), timeout=10.0)
        except asyncio.TimeoutError:
            return None
        if not line:
            return None
        try:
            return GatewayFrame.decode(line)
        except Exception:
            logger.warning("Failed to decode registration response", exc_info=True)
            return None

    async def reconnect_until_registered(
        self,
        *,
        session_id: str | None = None,
        origin: str | None = None,
        capabilities: list[str] | None = None,
        token: str | None = None,
    ) -> GatewayFrame | None:
        """(Re)connect and register, returning the ACK/NACK frame.

        Best-effort for initial gateway unavailability: a failed
        connection or registration returns ``None`` so callers can
        retry later. Any previous connection is torn down first so
        repeated calls never leak writers or heartbeat tasks.
        """
        try:
            await self.stop()
        except Exception:
            pass
        try:
            await self.connect()
        except Exception:
            logger.debug("Gateway connect failed", exc_info=True)
            return None
        try:
            return await self.register(
                session_id=session_id,
                origin=origin,
                capabilities=capabilities,
                token=token,
            )
        except Exception:
            logger.debug("Gateway register failed", exc_info=True)
            return None

    async def send(self, message: InboundMessage) -> None:
        if self._writer is None:
            raise GatewayIpcError("Not connected")
        frame = GatewayFrame.outbound(
            origin=message.origin, text=message.text,
            context_token=message.context_token,
            metadata=message.metadata if message.metadata else None,
            semantic_tags=message.semantic_tags,
        )
        self._writer.write(frame.encode())
        await self._writer.drain()

    async def stop(self) -> None:
        self._running = False
        for task in (self._heartbeat_task, self._read_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def close(self) -> None:
        """Alias for stop() for callers using the close-style lifecycle."""
        await self.stop()

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if self._writer and not self._writer.is_closing():
                    frame = GatewayFrame.heartbeat(session_id=self._instance_id)
                    self._writer.write(frame.encode())
                    await self._writer.drain()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Heartbeat failed", exc_info=True)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while self._running:
            try:
                line = await self._reader.readline()
            except Exception:
                if self._running:
                    logger.warning("Gateway read error", exc_info=True)
                break
            if not line:
                logger.info("Gateway connection closed")
                break
            try:
                frame = GatewayFrame.decode(line)
            except Exception:
                logger.warning("Failed to decode gateway frame", exc_info=True)
                continue
            if frame.type == FrameType.DELIVER and self.on_deliver:
                msg = InboundMessage(
                    origin=frame.origin or "", text=frame.text or "",
                    message_id=frame.delivery_id or "", channel_type="gateway",
                    semantic=frame.semantic, context_token=frame.context_token,
                    semantic_tags=frame.semantic_tags,
                    metadata=frame.metadata or {},
                )
                try:
                    self.on_deliver(msg)
                except Exception:
                    logger.exception("on_deliver callback failed")
