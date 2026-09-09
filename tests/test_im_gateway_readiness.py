"""Regression coverage for the six gateway readiness findings.

The fixture uses the real gateway, UDS protocol and Feishu adapter. Only
provider network I/O is replaced; no external messages or agent runs occur.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestratord.channels.capabilities import ProcessingOutcome
from orchestratord.channels.feishu_app import FeishuAppChannelAdapter
from orchestratord.channels.feishu_events import translate_inbound
from orchestratord.channels.health import channel_health_ready, channel_status_ready
from orchestratord.channels.models import ChannelConfig, ChannelType
from orchestratord.channels.results import (
    ChannelHealth,
    ChannelSendResult,
    ErrorCategory,
)
from orchestratord.commands.models import CommandResult
from orchestratord.commands.service import OrchestratorCommandService
from orchestratord.im_gateway.config import GatewayConfig, save_config
from orchestratord.im_gateway.gateway import MessageGateway
from orchestratord.im_gateway_client import (
    OrchestratorGatewayClient,
    OrchestratorHandlers,
)
from orchestratord.ipc.client import GatewayIpcClient
from orchestratord.ipc.models import InboundMessage, MessageSemantics, OutboundMessage
from orchestratord.ipc.protocol import FrameType

ORIGIN = "feishu:dm:cli_test:ou_allowed"


class _Provider:
    def __init__(self):
        self.sent = []

    async def send(self, to, message, opts=None):
        self.sent.append((to, message))
        return SimpleNamespace(success=True, message_id="fake-receipt", raw={})

    async def disconnect(self):
        pass


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def link(tmp_path):
    from orchestratord.im_gateway.ipc_server import GatewayIpcServer

    gw = MessageGateway(GatewayConfig(state_dir=str(tmp_path)))
    config = ChannelConfig(
        name="feishu",
        type=ChannelType.FEISHU,
        extra={
            "connection_mode": "websocket",
            "app_id": "cli_test",
            "app_secret": "test-only",
            "allowed_user_open_id": "ou_allowed",
        },
    )
    provider = _Provider()
    adapter = FeishuAppChannelAdapter(config)
    adapter._channel = provider
    adapter.on_processing_start = AsyncMock(return_value=True)
    adapter.on_processing_complete = AsyncMock(return_value=True)
    gw.registry.register(adapter)
    gw.config.channels = [config]
    server = GatewayIpcServer(tmp_path / "g.sock", gw)
    await server.start()
    client = GatewayIpcClient(str(server.socket_path), instance_id="orchestrator:probe")
    calls = []
    handlers = OrchestratorHandlers(
        **{
            name: (lambda *args, name=name: calls.append((name, args)))
            for name in OrchestratorHandlers.__dataclass_fields__
        }
    )
    wrapper = OrchestratorGatewayClient(
        handlers,
        ipc_client=client,
        origin="im:direct:*:*",
        command_service=SimpleNamespace(execute=AsyncMock(return_value=CommandResult(0, "ok"))),
    )

    async def push(message):
        return await server.push_deliver(
            origin=message.origin,
            delivery_id=message.message_id,
            text=message.text,
            semantic=message.semantic.value,
            context_token=message.context_token,
        )

    async def inbound(text, mid="m1", sender="ou_allowed"):
        msg = translate_inbound(
            SimpleNamespace(
                id=mid,
                sender=SimpleNamespace(open_id=sender),
                content_text=text,
                conversation=SimpleNamespace(chat_type="p2p", chat_id="oc_test"),
            ),
            adapter._settings,
        )
        return await gw._on_inbound(msg) if msg is not None else None

    gw.set_push_handler(push)
    await client.connect()
    await client.register(origin="im:direct:*:*", capabilities=["orchestrator"])
    # Drop the independent connection notification before making assertions.
    await asyncio.sleep(0.05)
    provider.sent.clear()
    try:
        yield SimpleNamespace(
            gw=gw,
            adapter=adapter,
            provider=provider,
            server=server,
            client=client,
            wrapper=wrapper,
            calls=calls,
            inbound=inbound,
            config=config,
            tmp=tmp_path,
        )
    finally:
        await client.stop()
        task = wrapper._deliver_flush_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await server.close()
        await gw.stop()


@pytest.mark.parametrize("target", ["ou_allowed", "ou_denied", None])
async def test_final_send_checks_recipient_allowlist(link, target):
    result = await link.gw.send(
        OutboundMessage(channel="feishu", target=target, text="report")
    )
    assert result.ok is (target == "ou_allowed")
    assert len(link.provider.sent) == int(target == "ou_allowed")
    if not result.ok:
        assert result.error_category is ErrorCategory.AUTH
        assert link.gw.store.dead_letter_entries()


async def test_unauthorized_concrete_outbound_is_nacked(link):
    response = await link.client.send_outbound(
        origin="feishu:dm:cli_test:ou_denied", text="private report"
    )
    assert response.type is FrameType.NACK
    assert link.provider.sent == []


async def test_feishu_context_cannot_redirect_authorized_reply(link):
    response = await link.client.send_outbound(
        origin=ORIGIN, text="private report", context_token="oc_unrelated"
    )
    assert response.type is FrameType.ACK
    assert link.provider.sent[0][0] == "ou_allowed"


@pytest.mark.parametrize(
    "verb",
    [
        "show",
        "tail",
        "stop",
        "pause",
        "resume",
        "clarify",
        "inject",
        "feedback",
        "review",
        "retry",
        "workspace",
        "rebase",
    ],
)
async def test_issue_commands_require_explicit_id(link, verb):
    await link.inbound(f"/issue {verb}")
    await _until(lambda: link.adapter.on_processing_complete.await_count)
    link.adapter.on_processing_complete.assert_awaited_once_with(
        "m1", ProcessingOutcome.FAILURE
    )
    assert link.calls == []


async def test_real_cli_show_roundtrip(link, monkeypatch):
    from orchestratord.issue_registry import IssueRegistry

    registry = IssueRegistry(link.tmp / ".orchestratord_issue_registry.json")
    registry.register("I1", "Gateway integration fixture")
    monkeypatch.setenv("ORCHESTRATORD_WORKSPACE_ROOT", str(link.tmp))
    link.wrapper._command_service = OrchestratorCommandService(workspace_root=link.tmp)
    await link.inbound("/issue show --id I1")
    await _until(lambda: link.adapter.on_processing_complete.await_count)
    await _until(lambda: link.provider.sent)
    link.adapter.on_processing_complete.assert_awaited_once_with(
        "m1", ProcessingOutcome.SUCCESS
    )
    assert "Gateway integration fixture" in str(link.provider.sent)


async def test_revoked_recipient_cannot_receive_persisted_outbox(link):
    link.gw.store.append_outbox(
        {
            "idempotency_key": "pending-before-revoke",
            "status": "pending",
            "at": time.time(),
            "channel": "feishu",
            "target": "ou_revoked",
            "text": "private report",
        }
    )
    await link.gw._replay_pending_outbox()
    assert link.provider.sent == []
    assert link.gw.store.outbox_pending() == []
    assert link.gw.store.dead_letter_entries()[-1]["error_category"] == "auth"


async def test_retry_checks_replacement_adapter_authorization(link):
    first = AsyncMock(
        return_value=ChannelSendResult.retryable_error(
            "feishu", message="temporary failure", category=ErrorCategory.NETWORK
        )
    )
    link.adapter.send = first
    replacement = FeishuAppChannelAdapter(
        replace(
            link.config,
            extra={
                **link.config.extra,
                "allowed_user_open_id": "ou_new",
            },
        )
    )
    replacement.send = AsyncMock()

    async def replace_during_backoff(_delay):
        link.gw.registry.register(replacement)

    link.gw.outbound._sleep = replace_during_backoff
    result = await link.gw.send(
        OutboundMessage(channel="feishu", target="ou_allowed", text="report")
    )
    assert result.error_category is ErrorCategory.AUTH
    first.assert_awaited_once()
    replacement.send.assert_not_awaited()


async def test_authorized_inbound_executes_real_pause_handler_once_and_replies(link):
    from orchestratord.issue_registry import IssueRegistry
    from orchestratord.orchestrator import Orchestrator

    registry = IssueRegistry(link.tmp / "registry.json")
    registry.register("I1", "I1")
    session = SimpleNamespace(
        status="running",
        pause_resume_event=asyncio.Event(),
        _pause_gate=None,
        state_cache=None,
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch._state = SimpleNamespace(running={"I1": session})
    orch._registry = registry
    orch._im_emitters = {}
    orch.im_event_deliver = None
    calls = []

    def control(verb, issue_id, extra):
        calls.append((verb, issue_id))
        Orchestrator._apply_control_command(orch, verb, issue_id, extra)

    orch._apply_control_command = control
    link.wrapper._command_service = OrchestratorCommandService(workspace_root=link.tmp, runtime_supplier=lambda: orch)
    await link.inbound("/issue pause --id I1")
    await _until(lambda: link.provider.sent)
    await link.inbound("/issue pause --id I1")  # same message id: deduplicated
    assert session.paused
    assert registry.get("I1").pause_reason
    assert calls == [("pause", "I1")]
    assert link.provider.sent[-1][0] == "ou_allowed"


@pytest.mark.parametrize("text", ["hello", "/agent follow-up I1", "/pause I1"])
async def test_rejected_channel_messages_never_reach_handlers(link, text):
    ack = await link.inbound(text)
    assert ack.notify_user
    assert link.calls == []
    assert await link.inbound("/issue pause --id I1", sender="ou_denied") is None
    assert link.calls == []


@pytest.mark.parametrize(
    "semantic", [s for s in MessageSemantics if s is not MessageSemantics.COMMAND]
)
async def test_client_rejects_semantic_frames_even_with_authenticated_origin(
    link, semantic
):
    await link.server.push_deliver(
        origin=ORIGIN,
        delivery_id="semantic",
        text="/issue pause --id I1",
        semantic=semantic.value,
    )
    await asyncio.sleep(0.05)
    assert link.calls == []
    assert link.provider.sent == []


@pytest.mark.parametrize(
    "origin,metadata",
    [
        (ORIGIN, {}),
        (ORIGIN, {"authenticated_origin": "feishu:dm:cli_test:ou_other"}),
        ("", {"authenticated_origin": ""}),
        ("im:direct:*:*", {"authenticated_origin": "im:direct:*:*"}),
    ],
)
async def test_client_rejects_unverified_origins(link, origin, metadata):
    status = await link.wrapper.dispatch(
        InboundMessage(
            origin=origin,
            text="/issue pause --id I1",
            metadata=metadata,
        ),
        MessageSemantics.COMMAND,
    )
    assert status == "origin_unauthenticated"
    assert link.calls == []


async def test_gateway_will_not_stamp_a_different_account(link):
    assert not await link.server.push_deliver(
        origin="feishu:dm:wrong_account:ou_allowed",
        delivery_id="wrong",
        text="/issue pause --id I1",
        semantic="command",
    )
    assert link.calls == []


async def test_slow_command_keeps_heartbeat_and_outbound_acks_readable(link):
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(_argv):
        entered.set()
        await release.wait()
        return 0, "query result", ""

    link.wrapper._run_cli_isolated = run
    await link.inbound("/server status")
    await entered.wait()
    link.client._reply_timeout = 0.2
    try:
        assert (await link.client.heartbeat()).type is FrameType.ACK
        assert (
            await link.client.send_outbound(origin=ORIGIN, text="event")
        ).type is FrameType.ACK
    finally:
        release.set()
    await _until(lambda: link.adapter.on_processing_complete.await_count == 1)


async def test_deliver_worker_serializes_and_drops_queued_commands_on_disconnect(link):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(argv):
        calls.append(argv)
        entered.set()
        await release.wait()
        return 0, "ok", ""

    link.wrapper._run_cli_isolated = run
    await link.inbound("/server status", "first")
    await entered.wait()
    await link.inbound("/issue list", "second")
    await _until(lambda: link.client._deliver_queue.qsize() == 1)
    assert len(calls) == 1
    await link.client.stop()
    release.set()
    assert link.client._deliver_queue.empty()
    assert link.client._deliver_task is None
    assert len(calls) == 1


@pytest.mark.parametrize("rc", [0, 2, 124])
async def test_processing_outcome_follows_command_result_not_reply_delivery(link, rc):
    link.wrapper._command_service = SimpleNamespace(execute=AsyncMock(
        return_value=CommandResult(rc, "ok" if not rc else "", "failed" if rc else "")))
    await link.inbound("/server status")
    await _until(lambda: link.adapter.on_processing_complete.await_count)
    await _until(lambda: link.provider.sent)
    await asyncio.sleep(0.05)
    expected = ProcessingOutcome.FAILURE if rc else ProcessingOutcome.SUCCESS
    link.adapter.on_processing_complete.assert_awaited_once_with("m1", expected)


@pytest.mark.parametrize(
    "status",
    ["disconnected", "websocket:disconnected", "reconnecting", "not_logged_in"],
)
async def test_unhealthy_channels_never_count_as_ready(link, status):
    assert not channel_status_ready(status)
    health = ChannelHealth(healthy=False, channel_id="feishu", account_status=status)
    assert not channel_health_ready(health)
    link.adapter.health_check = AsyncMock(return_value=health)
    assert not await link.gw._wait_adapter_ready(link.adapter, 0)
    link.gw._inbound_adapters = [link.adapter]
    assert (await link.gw.wait_channels_ready(timeout=0))["feishu"] == status


@pytest.mark.parametrize(
    "mode", ["missing_secret", "disconnected", "exception", "ready"]
)
async def test_reload_commits_only_a_valid_healthy_replacement(link, mode):
    config = link.config
    if mode == "missing_secret":
        config = replace(
            config,
            extra={**config.extra, "app_secret": "${READINESS_TEST_MISSING_SECRET}"},
        )
    config_path = save_config(
        GatewayConfig(state_dir=str(link.tmp), channels=[config]),
        link.tmp / "channels.yaml",
    )
    replacement = FeishuAppChannelAdapter(
        config, channel_factory=lambda settings: _Provider()
    )
    replacement.start = AsyncMock(
        side_effect=RuntimeError("start failed") if mode == "exception" else None
    )
    replacement.stop = AsyncMock()
    replacement.health_check = AsyncMock(
        return_value=ChannelHealth(
            healthy=mode == "ready",
            channel_id="feishu",
            account_status="websocket:connected"
            if mode == "ready"
            else "websocket:disconnected",
        )
    )
    link.gw._config_path = config_path
    link.gw._build_adapter = lambda cfg: replacement
    link.gw._running = True
    original_config = link.gw.config
    ok = await link.gw.reload_channel("feishu", ready_timeout=0)
    assert ok is (mode == "ready")
    assert link.gw.registry.get("feishu") is (replacement if ok else link.adapter)
    if not ok:
        assert link.gw.config is original_config
        replacement.stop.assert_awaited_once()


async def test_cancelled_reload_stops_replacement_and_keeps_old_channel(link):
    replacement = FeishuAppChannelAdapter(link.config)
    entered = asyncio.Event()

    async def start():
        entered.set()
        await asyncio.Event().wait()

    replacement.start = start
    replacement.stop = AsyncMock()
    link.gw._config_path = save_config(
        GatewayConfig(channels=[link.config]), link.tmp / "channels.yaml"
    )
    link.gw._build_adapter = lambda cfg: replacement
    link.gw._running = True
    task = asyncio.create_task(link.gw.reload_channel("feishu"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert link.gw.registry.get("feishu") is link.adapter
    replacement.stop.assert_awaited_once()


async def test_reload_bounds_a_stuck_startup_and_keeps_old_channel(link):
    replacement = FeishuAppChannelAdapter(link.config)

    async def start():
        await asyncio.Event().wait()

    replacement.start = start
    replacement.stop = AsyncMock()
    link.gw._config_path = save_config(
        GatewayConfig(channels=[link.config]), link.tmp / "channels.yaml"
    )
    link.gw._build_adapter = lambda cfg: replacement
    link.gw._running = True
    assert not await asyncio.wait_for(
        link.gw.reload_channel("feishu", ready_timeout=0.01), timeout=1
    )
    assert link.gw.registry.get("feishu") is link.adapter
    replacement.stop.assert_awaited_once()


async def test_reload_waits_beyond_normal_ack_budget_for_terminal_result(link):
    async def reload(_name):
        await asyncio.sleep(0.05)
        return False

    link.gw.reload_channel = reload
    link.client._reply_timeout = 0.01
    response = await link.client.reload_channel("feishu")
    assert response.type is FrameType.NACK
    assert "previous channel retained" in response.reason
