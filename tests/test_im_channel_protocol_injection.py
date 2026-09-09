"""Phase 3: OrchestratorGatewayClient Protocol/dependency-injection tests.

Focus on the existing injectable seams:
  * ``command_router`` / ``control_bridge`` default to shim objects.
  * Commands are delegated to an injected in-process application service.
  * ``ipc_client`` and ``handlers`` are wired without touching upstream code.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestratord.im_gateway_client import (
    OrchestratorGatewayClient,
    OrchestratorHandlers,
)
from orchestratord.ipc.client import GatewayIpcClient
from orchestratord.ipc.models import InboundMessage
from orchestratord.ipc.protocol import GatewayFrame


@dataclass
class StubCommandRouter:
    marker: str = "stub-router"

    def route(self, message: Any) -> Any | None:
        return None


@dataclass
class StubControlBridge:
    marker: str = "stub-bridge"

    def resolve(self, semantic: Any, route: Any) -> Any | None:
        return None


def _noop_handlers() -> OrchestratorHandlers:
    return OrchestratorHandlers(
        queue_pending_message=lambda _i, _t: None,
        control_verb=lambda _v, _i: None,
        issue_inject=lambda _i, _h: None,
        operator_hints=lambda _i, _t: None,
        agent_intent=lambda _v, _i: None,
        issue_cli=lambda _v, _i, _p: None,
        bridge_interrupt=lambda _i, _p: None,
    )


def test_default_uses_compat_shim_instances() -> None:
    """When no router/bridge provided, the constructor falls back to
    clawcodex_compat shims (CommandRouter / ControlBridge)."""
    client = OrchestratorGatewayClient(_noop_handlers())

    assert type(client._commands).__name__ == "CommandRouter"
    assert type(client._control).__name__ == "ControlBridge"
    assert type(client._command_service).__name__ == "OrchestratorCommandService"


def test_injected_router_and_bridge_are_used() -> None:
    """command_router / control_bridge can be replaced with test doubles."""
    router = StubCommandRouter()
    bridge = StubControlBridge()
    client = OrchestratorGatewayClient(
        _noop_handlers(),
        command_router=router,
        control_bridge=bridge,
    )

    assert client._commands is router
    assert client._control is bridge


@pytest.mark.asyncio
async def test_ipc_deliver_routes_to_dispatch() -> None:
    """Server-pushed DELIVER frames are converted and dispatched without
    importing an optional backend package at call time."""
    client = OrchestratorGatewayClient(
        _noop_handlers(),
        origin="im:direct:test:*",
    )
    client.dispatch = AsyncMock(return_value="orchestrator_cli_issue_list")  # type: ignore[method-assign]

    ipc = AsyncMock()
    ipc_client = MagicMock()
    ipc_client.on_deliver = None
    client._ipc = ipc

    frame = MagicMock()
    frame.origin = "im:direct:test:room"
    frame.text = "/agent retry 42"
    frame.semantic = "command"
    frame.delivery_id = "DEL-123"

    await client._on_pushed_deliver(frame)

    client.dispatch.assert_called_once()
    call_args = client.dispatch.call_args
    message, semantic = call_args.args
    assert message.text == "/agent retry 42"
    assert message.origin == "im:direct:test:room"
    assert semantic.value == "command"
    ipc.complete_processing.assert_awaited_once_with(
        message_id="DEL-123",
        outcome="success",
        reason="orchestrator_cli_issue_list",
    )


@pytest.mark.asyncio
async def test_ipc_client_awaits_async_delivery_handler() -> None:
    """The normalized IPC delivery reaches an async orchestrator callback."""
    client = GatewayIpcClient("unused.sock", "test-instance")
    delivered: list[InboundMessage] = []
    handled = asyncio.Event()

    async def on_deliver(message: InboundMessage) -> None:
        delivered.append(message)
        handled.set()

    class Reader:
        def __init__(self) -> None:
            self._lines = iter((
                GatewayFrame.deliver(
                    delivery_id="DEL-42",
                    session_id="test-instance",
                    origin="im:direct:test:room",
                    text="continue",
                    semantic="followUp",
                ).encode(),
                b"",
            ))

        async def readline(self) -> bytes:
            line = next(self._lines)
            if not line:
                await asyncio.wait_for(handled.wait(), timeout=1)
            return line

    client._reader = Reader()  # type: ignore[assignment]
    client._running = True
    client.on_deliver = on_deliver

    await client._read_loop()

    assert [(message.message_id, message.text, message.semantic) for message in delivered] == [
        ("DEL-42", "continue", "followUp"),
    ]
