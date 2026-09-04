"""GatewayIpcClient ↔ fake UDS gateway server tests.

Runs a minimal JSONL gateway over a real Unix domain socket (via
``asyncio.start_unix_server``) and exercises the client's request/reply
correlation, server-pushed DELIVER normalization, fire-and-forget frames,
write serialization, and waiter release on close.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

from orchestratord.ipc.client import GatewayIpcClient
from orchestratord.ipc.models import InboundMessage
from orchestratord.ipc.protocol import FrameType, GatewayFrame


class _FakeGatewayServer:
    """Minimal JSONL gateway daemon for exercising the real IPC client.

    Replies follow the same id-echo convention as the gateway server:
    ACK/NACK frames carry the request's ``message_id`` in their
    ``delivery_id`` field.
    """

    def __init__(
        self,
        sock_path: Path,
        *,
        outbound_reply: Callable[[GatewayFrame], GatewayFrame | None] | None = None,
        reply_delay: float = 0.0,
    ) -> None:
        self.sock_path = sock_path
        self._outbound_reply = outbound_reply
        self._reply_delay = reply_delay
        self.frames: list[GatewayFrame] = []
        self.server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(
            self._handle_client, str(self.sock_path)
        )

    async def close(self) -> None:
        # Close accepted writers BEFORE wait_closed(): on Python >=3.12
        # wait_closed() blocks until every handler finished, and a handler
        # parked in readline() only finishes once its connection is gone.
        if self.server is not None:
            self.server.close()
        for writer in list(self._writers):
            if not writer.is_closing():
                writer.close()
        if self.server is not None:
            await self.server.wait_closed()
            self.server = None
        self._writers.clear()

    async def push_deliver(
        self,
        *,
        delivery_id: str,
        origin: str,
        text: str,
        semantic: str | None = None,
        context_token: str | None = None,
    ) -> None:
        frame = GatewayFrame.deliver(
            delivery_id=delivery_id,
            session_id="gateway",
            origin=origin,
            text=text,
            semantic=semantic,
            context_token=context_token,
        )
        for writer in list(self._writers):
            if writer.is_closing():
                continue
            try:
                writer.write(frame.encode())
                await writer.drain()
            except (ConnectionError, OSError):
                pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = GatewayFrame.decode(line)
                except ValueError:
                    # Undecodable line: record nothing — tests assert the
                    # exact set of well-formed frames received.
                    continue
                self.frames.append(frame)
                if self._reply_delay:
                    await asyncio.sleep(self._reply_delay)
                reply = self._reply_for(frame)
                if reply is not None:
                    writer.write(reply.encode())
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.close()
            if writer in self._writers:
                self._writers.remove(writer)

    def _reply_for(self, frame: GatewayFrame) -> GatewayFrame | None:
        if frame.type is FrameType.REGISTER:
            return GatewayFrame.ack(
                delivery_id=frame.message_id, layer="accepted", message="registered"
            )
        if frame.type is FrameType.HEARTBEAT:
            return GatewayFrame.ack(delivery_id=frame.message_id, layer="accepted")
        if frame.type is FrameType.UNREGISTER:
            return GatewayFrame.ack(delivery_id=frame.message_id, layer="accepted")
        if frame.type is FrameType.OUTBOUND:
            if self._outbound_reply is not None:
                return self._outbound_reply(frame)
            return GatewayFrame.ack(delivery_id=frame.message_id, layer="processed")
        if frame.type is FrameType.EVENT:
            if frame.event_type == "control.status":
                return GatewayFrame(
                    type=FrameType.ACK,
                    delivery_id=frame.message_id,
                    ack_layer="accepted",
                    payload={"running": True, "channels": ["wechat"]},
                )
            return GatewayFrame.ack(delivery_id=frame.message_id, layer="accepted")
        return None


async def _wait_for_frames(server: _FakeGatewayServer, count: int) -> None:
    for _ in range(200):
        if len(server.frames) >= count:
            return
        await asyncio.sleep(0.01)


async def test_register_receives_accepted_ack_and_marks_running(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-1")
    try:
        assert client.socket_path == str(sock)
        await client.connect()
        response = await client.register(origin="im:direct:*:*", capabilities=["outbound_text"])
        assert response is not None
        assert response.type is FrameType.ACK
        assert response.ack_layer == "accepted"
        assert client._running is True

        hb = await client.heartbeat()
        assert hb is not None
        assert hb.ack_layer == "accepted"

        registers = [f for f in server.frames if f.type is FrameType.REGISTER]
        assert registers and registers[0].session_id == "orchestrator-1"
        assert registers[0].origin == "im:direct:*:*"
        assert registers[0].capabilities == ["outbound_text"]
    finally:
        await client.close()
        await server.close()


async def test_register_passes_token_to_register_frame(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-2", token="pairing-token")
    try:
        await client.connect()
        response = await client.register()
        assert response is not None
        assert response.ack_layer == "accepted"
        registers = [f for f in server.frames if f.type is FrameType.REGISTER]
        assert registers and registers[0].token == "pairing-token"
    finally:
        await client.close()
        await server.close()


async def test_send_outbound_returns_ack_and_nack_frames(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"

    def _outbound_reply(frame: GatewayFrame) -> GatewayFrame | None:
        if frame.text == "nack me":
            return GatewayFrame.nack(delivery_id=frame.message_id, reason="rate limited")
        return GatewayFrame.ack(delivery_id=frame.message_id, layer="processed")

    server = _FakeGatewayServer(sock, outbound_reply=_outbound_reply)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-3")
    try:
        await client.connect()
        ack = await client.send_outbound(
            origin="wechat:direct:acct:user_zhao", text="reply from agent"
        )
        assert ack is not None
        assert ack.type is FrameType.ACK
        assert ack.ack_layer == "processed"

        nack = await client.send_outbound(
            origin="wechat:direct:acct:user_zhao", text="nack me"
        )
        assert nack is not None
        assert nack.type is FrameType.NACK
        assert nack.reason == "rate limited"
    finally:
        await client.close()
        await server.close()


async def test_send_outbound_without_reply_times_out_to_none(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock, outbound_reply=lambda _frame: None)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-4", reply_timeout=0.1)
    try:
        await client.connect()
        response = await client.send_outbound(origin="im:direct:*:*", text="no reply")
        assert response is None
        # The request frame still reached the gateway.
        outbound = [f for f in server.frames if f.type is FrameType.OUTBOUND]
        assert outbound and outbound[0].text == "no reply"
        assert client._pending == {}
    finally:
        await client.close()
        await server.close()


async def test_server_push_deliver_normalizes_inbound_message(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    delivered: list[InboundMessage] = []

    async def on_deliver(message: InboundMessage) -> None:
        delivered.append(message)

    client = GatewayIpcClient(str(sock), "orchestrator-5", on_deliver=on_deliver)
    try:
        await client.connect()
        response = await client.register(origin="im:direct:*:*")
        assert response is not None and response.ack_layer == "accepted"

        await server.push_deliver(
            delivery_id="DEL-7",
            origin="wechat:direct:acct:user_zhao",
            text="hello from wechat",
            semantic="followUp",
            context_token="ctx_abc",
        )
        for _ in range(100):
            if delivered:
                break
            await asyncio.sleep(0.01)

        assert len(delivered) == 1
        msg = delivered[0]
        assert isinstance(msg, InboundMessage)
        assert msg.channel_type == "gateway"
        assert msg.message_id == "DEL-7"
        assert msg.origin == "wechat:direct:acct:user_zhao"
        assert msg.text == "hello from wechat"
        assert msg.semantic == "followUp"
        assert msg.context_token == "ctx_abc"
    finally:
        await client.close()
        await server.close()


async def test_complete_processing_emits_processing_complete_event(
    tmp_path: Path,
) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-6")
    try:
        await client.connect()
        await client.register(origin="im:direct:*:*")

        result = await client.complete_processing(
            message_id="DEL-9", outcome="success", reason="followup_queued"
        )
        assert result is None

        await _wait_for_frames(server, 2)
        events = [f for f in server.frames if f.type is FrameType.EVENT]
        assert events
        assert events[0].event_type == "processing.complete"
        assert events[0].payload == {
            "message_id": "DEL-9",
            "outcome": "success",
            "reason": "followup_queued",
        }
    finally:
        await client.close()
        await server.close()


async def test_concurrent_send_outbound_does_not_interleave_frames(
    tmp_path: Path,
) -> None:
    sock = tmp_path / "gw.sock"
    # Delayed replies keep both requests in flight concurrently.
    server = _FakeGatewayServer(sock, reply_delay=0.02)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-7")
    try:
        await client.connect()
        texts = ["alpha message", "beta message"]
        responses = await asyncio.gather(
            client.send_outbound(origin="im:direct:*:*", text=texts[0]),
            client.send_outbound(origin="im:direct:*:*", text=texts[1]),
        )
        # Both replies are routed back to their own pending request.
        assert all(r is not None and r.type is FrameType.ACK for r in responses)
        assert all(r.ack_layer == "processed" for r in responses)
        # Interleaved writes would produce undecodable lines that the
        # server drops, so exactly two well-formed OUTBOUND frames must be
        # recorded, one per text.
        outbound = [f for f in server.frames if f.type is FrameType.OUTBOUND]
        assert sorted(f.text for f in outbound) == sorted(texts)
        assert client._pending == {}
    finally:
        await client.close()
        await server.close()


async def test_close_releases_pending_outbound_waiter(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock, outbound_reply=lambda _frame: None)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-8", reply_timeout=5.0)
    try:
        await client.connect()
        outbound = asyncio.create_task(
            client.send_outbound(origin="im:direct:*:*", text="stuck waiting")
        )
        # Let the write land so the future is parked in _pending.
        await asyncio.sleep(0.05)
        assert client._pending

        await client.close()
        # close() must release the waiter well before its own 5s timeout.
        response = await asyncio.wait_for(outbound, timeout=1.0)
        assert response is None
        assert client._pending == {}
    finally:
        await client.close()
        await server.close()


async def test_control_helpers_round_trip(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-9")
    try:
        await client.connect()
        status = await client.status()
        assert status == {"running": True, "channels": ["wechat"]}

        reload_resp = await client.reload_channel("wechat")
        assert reload_resp is not None
        assert reload_resp.ack_layer == "accepted"

        unbind_resp = await client.unbind_origin("wechat:direct:*:*")
        assert unbind_resp is not None
        assert unbind_resp.ack_layer == "accepted"

        events = [
            f for f in server.frames if f.type is FrameType.EVENT and f.payload is not None
        ]
        sent = [(f.event_type, f.payload) for f in events]
        assert ("control.reload", {"channel": "wechat"}) in sent
        assert ("control.unbind", {"origin": "wechat:direct:*:*"}) in sent
    finally:
        await client.close()
        await server.close()


async def test_async_context_manager_connects_and_closes(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    try:
        async with GatewayIpcClient(str(sock), "orchestrator-10") as client:
            response = await client.register(origin="im:direct:*:*")
            assert response is not None and response.ack_layer == "accepted"
            assert client._read_task is not None
        assert client._read_task is None
        assert client._writer is None
    finally:
        await server.close()


async def test_unregister_writes_frame_then_stops(tmp_path: Path) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-11")
    try:
        await client.connect()
        await client.register(origin="im:direct:*:*")
        await _wait_for_frames(server, 1)

        await client.unregister()
        await _wait_for_frames(server, 2)

        unregisters = [f for f in server.frames if f.type is FrameType.UNREGISTER]
        assert unregisters and unregisters[0].session_id == "orchestrator-11"
        assert client._writer is None
    finally:
        await client.close()
        await server.close()


async def test_reconnect_until_registered_returns_ack_on_live_gateway(
    tmp_path: Path,
) -> None:
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-12")
    try:
        response = await client.reconnect_until_registered(
            origin="im:direct:*:*", capabilities=["outbound_text"]
        )
        assert response is not None
        assert response.ack_layer == "accepted"
    finally:
        await client.close()
        await server.close()


async def test_reconnect_until_registered_defaults_to_single_attempt(
    tmp_path: Path,
) -> None:
    """The default is one attempt: missing sockets return None immediately
    (the orchestrator daemon owns the outer retry loop)."""
    client = GatewayIpcClient(str(tmp_path / "missing.sock"), "orchestrator-13")
    response = await client.reconnect_until_registered(origin="im:direct:*:*")
    assert response is None
    assert client._read_task is None
    assert client._writer is None


async def test_read_loop_stops_on_connection_close(tmp_path: Path) -> None:
    """A server-side close ends the read loop without hanging the client."""
    sock = tmp_path / "gw.sock"
    server = _FakeGatewayServer(sock)
    await server.start()
    client = GatewayIpcClient(str(sock), "orchestrator-14")
    try:
        await client.connect()
        await client.register(origin="im:direct:*:*")
        read_task = client._read_task
        assert read_task is not None

        await server.close()
        await asyncio.wait_for(read_task, timeout=2.0)
        assert read_task.done()
    finally:
        await client.close()
        await server.close()
