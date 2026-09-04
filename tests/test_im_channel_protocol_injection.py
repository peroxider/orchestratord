"""Phase 3: OrchestratorGatewayClient Protocol/dependency-injection tests.

Focus on the existing injectable seams:
  * ``command_router`` / ``control_bridge`` default to shim objects.
  * ``cli_runner`` fully replaces the ``run_orchestrator_subcommand`` import.
  * ``ipc_client`` and ``handlers`` are wired without touching upstream code.
"""
from __future__ import annotations

import asyncio
import sys
import time
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
    assert client._cli_runner is None


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


def test_cli_runner_replaces_subcommand_import() -> None:
    """``cli_runner`` short-circuits the run_orchestrator_subcommand import."""
    captured: list[list[str]] = []

    def cli_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(argv)
        return 42, "stdout", "stderr"

    client = OrchestratorGatewayClient(
        _noop_handlers(),
        cli_runner=cli_runner,
    )
    rc, stdout, stderr = client._run_orchestrator_cli(["issue", "list"])

    assert rc == 42
    assert stdout == "stdout"
    assert stderr == "stderr"
    assert captured == [["issue", "list"]]


@pytest.mark.asyncio
async def test_default_cli_runner_uses_child_process_without_mutating_sys_argv(monkeypatch) -> None:
    """Production commands execute in a child and preserve daemon globals."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _Process:
        returncode = 3

        async def communicate(self):
            return b"stdout", b"stderr"

    async def _create(*argv, **kwargs):
        calls.append((argv, kwargs))
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    sentinel = ["orchestratord", "server", "start"]
    sys.argv = list(sentinel)
    client = OrchestratorGatewayClient(_noop_handlers())

    rc, stdout, stderr = await client._run_cli_isolated(["issue", "list"])

    assert rc == 3
    assert stdout == "stdout"
    assert stderr == "stderr"
    assert calls[0][0][0] == sys.executable
    assert calls[0][0][-2:] == ("issue", "list")
    assert sys.argv == sentinel


@pytest.mark.asyncio
async def test_default_cli_runner_reports_spawn_error(monkeypatch) -> None:
    async def _create(*_argv, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    client = OrchestratorGatewayClient(_noop_handlers())

    rc, stdout, stderr = await client._run_cli_isolated(["issue", "list"])

    assert rc == 1
    assert stdout == ""
    assert "boom" in stderr


@pytest.mark.asyncio
async def test_default_cli_runner_executes_real_child_process() -> None:
    client = OrchestratorGatewayClient(_noop_handlers(), cli_timeout_seconds=5.0)

    rc, stdout, stderr = await client._run_cli_isolated(["--version"])

    assert rc == 0
    assert stdout.startswith("orchestratord ")
    assert stderr == ""


@pytest.mark.asyncio
async def test_cli_commands_serialize_on_one_lock() -> None:
    """P2-6: concurrent commands execute strictly one-at-a-time (FIFO)."""
    active = 0
    max_active = 0
    order: list[str] = []

    def cli_runner(argv: list[str]) -> tuple[int, str, str]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.05)  # force overlap if serialization is missing
        order.append(argv[-1])
        active -= 1
        return 0, "", ""

    client = OrchestratorGatewayClient(_noop_handlers(), cli_runner=cli_runner)

    await asyncio.gather(
        client._run_cli_isolated(["issue", "list", "--tag", "a"]),
        client._run_cli_isolated(["issue", "list", "--tag", "b"]),
    )

    assert max_active == 1, "commands must be serialized"
    assert order == ["a", "b"]


@pytest.mark.asyncio
async def test_cli_command_times_out_with_rc_124() -> None:
    """P2-6: a command exceeding cli_timeout_seconds reports rc 124."""

    def slow_runner(argv: list[str]) -> tuple[int, str, str]:
        time.sleep(0.15)
        return 0, "late", ""

    client = OrchestratorGatewayClient(
        _noop_handlers(), cli_runner=slow_runner, cli_timeout_seconds=0.05
    )

    rc, stdout, stderr = await client._run_cli_isolated(["issue", "list"])

    assert rc == 124
    assert stdout == ""
    assert "timed out" in stderr


@pytest.mark.asyncio
async def test_cli_command_after_injected_timeout_waits_for_prior_worker() -> None:
    """The injectable thread runner may linger, but never overlaps its successor."""
    active = 0
    max_active = 0

    def runner(argv: list[str]) -> tuple[int, str, str]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if argv[-1] == "first":
            time.sleep(0.12)
        active -= 1
        return 0, argv[-1], ""

    client = OrchestratorGatewayClient(
        _noop_handlers(), cli_runner=runner, cli_timeout_seconds=0.03
    )

    first = await client._run_cli_isolated(["issue", "list", "first"])
    second = await client._run_cli_isolated(["issue", "list", "second"])

    assert first[0] == 124
    assert second == (0, "second", "")
    assert max_active == 1


@pytest.mark.asyncio
async def test_cli_subprocess_timeout_terminates_process(monkeypatch) -> None:
    class _Process:
        returncode = None
        terminated = False

        async def communicate(self):
            await asyncio.Event().wait()

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        async def wait(self):
            return self.returncode

    process = _Process()

    async def _create(*_argv, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    client = OrchestratorGatewayClient(_noop_handlers(), cli_timeout_seconds=0.03)

    rc, stdout, stderr = await client._run_cli_isolated(["issue", "list"])

    assert rc == 124
    assert stdout == ""
    assert "timed out" in stderr
    assert process.terminated is True


@pytest.mark.asyncio
async def test_ipc_deliver_routes_to_dispatch() -> None:
    """Server-pushed DELIVER frames are converted and dispatched without
    importing an optional backend package at call time."""
    client = OrchestratorGatewayClient(
        _noop_handlers(),
        origin="im:direct:test:*",
    )
    client.dispatch = AsyncMock(return_value="followup_queued")  # type: ignore[method-assign]

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
        reason="followup_queued",
    )


@pytest.mark.asyncio
async def test_ipc_client_awaits_async_delivery_handler() -> None:
    """The normalized IPC delivery reaches an async orchestrator callback."""
    client = GatewayIpcClient("unused.sock", "test-instance")
    delivered: list[InboundMessage] = []

    async def on_deliver(message: InboundMessage) -> None:
        delivered.append(message)

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
            return next(self._lines)

    client._reader = Reader()  # type: ignore[assignment]
    client._running = True
    client.on_deliver = on_deliver

    await client._read_loop()

    assert [(message.message_id, message.text, message.semantic) for message in delivered] == [
        ("DEL-42", "continue", "followUp"),
    ]
