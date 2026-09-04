"""Phase 4: orchestrator runtime alignment for the IM message gateway.

Covers the SPEC im-gateway migration Phase 4 behaviors:
  * FOLLOW_UP semantics reach the real follow-up control path
    (``.operator_hints.md`` + ``_handle_followup_control``).
  * COMMAND verbs land on the daemon's existing issue control handlers.
  * Blocked slash commands are ACKed to the user by the gateway
    (notify_user) instead of being pushed to the orchestrator.
  * The explicit ``OrchestrationSubsystem.orchestrator_ready`` hook
    replaces the former subsystem.run / Orchestrator.run monkey-patch.
  * DELIVER-triggered outbound flushes no longer block the IPC read loop.
  * Outbound events carry the issue_id/event_type/level/markdown metadata
    envelope and thread ``in_reply_to`` back to the triggering delivery.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestratord.events import EventLevel, OrchestratorEvent
from orchestratord.im_gateway.binding import BindingPolicy
from orchestratord.im_gateway.dispatcher import InboundDispatcher
from orchestratord.im_gateway.router import SessionRouter
from orchestratord.im_gateway.store import ReliabilityStore
from orchestratord.im_gateway_client import (
    OrchestratorGatewayClient,
    OrchestratorHandlers,
)
from orchestratord.ipc.models import AckLayer, InboundMessage, SessionTarget
from orchestratord.ipc.protocol import GatewayFrame
from orchestratord.sinks.channel import build_ipc_deliver

# -- fixtures -----------------------------------------------------------


def _noop_handlers(**overrides) -> OrchestratorHandlers:
    defaults = {
        "queue_pending_message": lambda _iid, _text: None,
        "control_verb": lambda _verb, _iid: None,
        "issue_inject": lambda _iid, _hint: None,
        "operator_hints": lambda _iid, _text: None,
        "agent_intent": lambda _verb, _iid: None,
        "issue_cli": lambda _verb, _iid, _payload: None,
        "bridge_interrupt": lambda _iid, _payload: None,
    }
    defaults.update(overrides)
    return OrchestratorHandlers(**defaults)


def _partial_orchestrator(tmp_path, *, record=None):
    """Build a partial Orchestrator with the IM-control attributes wired.

    Follows the repo's established ``Orchestrator.__new__`` test pattern:
    only the attributes touched by the IM control paths are populated.
    """
    from orchestratord.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._workspace_root = tmp_path
    orch._tasks = set()
    orch._im_emitters = {}
    orch.im_event_deliver = None

    issue_ws = tmp_path / "ws-issue"
    issue_ws.mkdir(exist_ok=True)
    orch._registry = SimpleNamespace(
        get=lambda issue_id: SimpleNamespace(workspace_path=str(issue_ws))
        if record is None and issue_id
        else record
    )
    from orchestratord.issue_clarifier.queue import ClarificationQueue

    orch._clarification_queue = ClarificationQueue(tmp_path / "cq.json")
    orch._handle_followup_control = AsyncMock()
    orch._handle_review_approve_control = AsyncMock()
    orch._handle_review_retry_control = AsyncMock()
    orch._handle_review_followup_control = AsyncMock()
    orch._handle_rebase_control = AsyncMock()
    orch._apply_control_command = MagicMock()
    return orch


class _RecordingIpc:
    """Fake IPC client with the full OUTBOUND frame signature."""

    def __init__(self) -> None:
        self.on_deliver = None
        self.sent: list[dict] = []
        self.complete_calls: list[tuple] = []

    async def send_outbound(self, *, origin, text, metadata=None, in_reply_to=None):
        self.sent.append(
            {
                "origin": origin,
                "text": text,
                "metadata": metadata,
                "in_reply_to": in_reply_to,
            }
        )
        return GatewayFrame.ack(delivery_id="ack", layer="processed", message="sent")

    async def complete_processing(self, *, message_id, outcome, reason):
        self.complete_calls.append((message_id, outcome, reason))


async def _drain(tasks: set) -> None:
    pending = [t for t in tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# -- FOLLOW_UP reaches the real follow-up control path -------------------


@pytest.mark.asyncio
async def test_followup_message_reaches_followup_control_path(tmp_path) -> None:
    """A followUp DELIVER writes operator hints and re-queues the issue via
    the existing _handle_followup_control handler."""
    orch = _partial_orchestrator(tmp_path)
    ipc = _RecordingIpc()
    client = OrchestratorGatewayClient(
        _noop_handlers(queue_pending_message=orch._apply_im_followup),
        ipc_client=ipc,
        origin="im:direct:*:*",
    )

    message = InboundMessage(
        origin="im:direct:acct:user",
        text="please also add tests",
        message_id="d-fu-1",
        channel_type="gateway",
        semantic="followUp",
        raw={"issue_id": "AGENTSDK-15"},
    )
    await client._on_pushed_deliver(message)
    await _drain(orch._tasks)

    orch._handle_followup_control.assert_awaited_once_with(
        "AGENTSDK-15", "please also add tests"
    )
    # The follow-up prompt text is recorded in the issue workspace's
    # .operator_hints.md (read by prompt_builder at launch time).
    hints = orch._registry.get("AGENTSDK-15").workspace_path
    from pathlib import Path

    hints_file = Path(hints) / ".operator_hints.md"
    assert "please also add tests" in hints_file.read_text(encoding="utf-8")
    # The delivery was still processed reliably.
    assert ipc.complete_calls and ipc.complete_calls[0][0] == "d-fu-1"


@pytest.mark.asyncio
async def test_followup_without_issue_id_records_hints_only(tmp_path) -> None:
    """Without an issue_id the hints file is the durable fallback — no
    follow-up control is scheduled."""
    orch = _partial_orchestrator(tmp_path)
    orch._apply_im_followup("", "remember to run ruff")

    orch._handle_followup_control.assert_not_awaited()
    hints_file = tmp_path / ".operator_hints.md"
    assert "remember to run ruff" in hints_file.read_text(encoding="utf-8")


# -- COMMAND verbs land on the issue control handlers --------------------


@pytest.mark.asyncio
async def test_review_command_reaches_review_approve_control(tmp_path) -> None:
    """/review --approve maps onto _handle_review_approve_control."""
    orch = _partial_orchestrator(tmp_path)
    client = OrchestratorGatewayClient(
        _noop_handlers(issue_cli=orch._apply_im_issue_cli),
        ipc_client=_RecordingIpc(),
        origin="im:direct:*:*",
    )

    message = InboundMessage(
        origin="im:direct:acct:user",
        text="/review AGENTSDK-15 --approve --comment LGTM",
        message_id="d-rv-1",
        channel_type="gateway",
        semantic="command",
    )
    await client._on_pushed_deliver(message)
    await _drain(orch._tasks)

    orch._handle_review_approve_control.assert_awaited_once_with("AGENTSDK-15", "LGTM")


@pytest.mark.asyncio
async def test_review_reject_reaches_review_retry_control(tmp_path) -> None:
    """/review --reject --feedback maps onto _handle_review_retry_control."""
    orch = _partial_orchestrator(tmp_path)
    orch._apply_im_issue_cli(
        "review", "AGENTSDK-15", '/review AGENTSDK-15 --reject --feedback "needs tests"'
    )
    await _drain(orch._tasks)

    orch._handle_review_retry_control.assert_awaited_once_with("AGENTSDK-15", "needs tests")


@pytest.mark.asyncio
async def test_feedback_command_reaches_review_followup_control(tmp_path) -> None:
    """/feedback --approve maps onto _handle_review_followup_control."""
    orch = _partial_orchestrator(tmp_path)
    orch._apply_im_issue_cli("feedback", "AGENTSDK-15", "/feedback AGENTSDK-15 --approve")
    await _drain(orch._tasks)

    orch._handle_review_followup_control.assert_awaited_once_with("AGENTSDK-15", "")


def test_issue_cli_retry_reaches_apply_control_command(tmp_path) -> None:
    """/issue retry maps onto the existing retry control handler."""
    orch = _partial_orchestrator(tmp_path)
    orch._apply_im_issue_cli(
        "retry", "AGENTSDK-15", "/issue retry --id AGENTSDK-15 --reason flaky"
    )

    orch._apply_control_command.assert_called_once_with("retry", "AGENTSDK-15", "flaky")


@pytest.mark.asyncio
async def test_issue_cli_rebase_reaches_rebase_control(tmp_path) -> None:
    """/issue rebase maps onto _handle_rebase_control with force/reason."""
    orch = _partial_orchestrator(tmp_path)
    orch._apply_im_issue_cli(
        "rebase", "AGENTSDK-15", "/issue rebase --id AGENTSDK-15 --force --reason drift"
    )
    await _drain(orch._tasks)

    orch._handle_rebase_control.assert_awaited_once_with("AGENTSDK-15", "force=1\ndrift")


@pytest.mark.asyncio
async def test_issue_cli_clarify_answers_clarification_queue(tmp_path) -> None:
    """/clarify --answer resolves the pending clarification item."""
    orch = _partial_orchestrator(tmp_path)
    orch._clarification_queue.inject_feedback("AGENTSDK-15", "which database?")

    orch._apply_im_issue_cli(
        "clarify", "AGENTSDK-15", '/clarify AGENTSDK-15 --answer "use sqlite"'
    )

    item = orch._clarification_queue.get("AGENTSDK-15")
    assert item is not None
    assert "use sqlite" in (item.answer or "")


# -- blocked slash commands are ACKed to the user by the gateway ---------


@pytest.mark.asyncio
async def test_blocked_orchestrator_command_acks_user_without_push(tmp_path) -> None:
    """A slash command outside the orchestrator allowlist is rejected at the
    gateway with notify_user=True and never pushed to the orchestrator."""
    store = ReliabilityStore(tmp_path)
    binding = BindingPolicy()
    binding.bind(
        "wechat:acct:op", SessionTarget(session_id="orch-sess", host_type="orchestrator")
    )
    router = SessionRouter(binding, store)
    pushed: list[InboundMessage] = []

    async def push_handler(message: InboundMessage) -> bool:
        pushed.append(message)
        return True

    dispatcher = InboundDispatcher(store, router)
    dispatcher.set_push_handler(push_handler)

    receipt = await dispatcher.process(
        InboundMessage(
            origin="wechat:acct:op",
            text="/deploy prod",
            message_id="mid-blocked-1",
            channel="wechat",
        )
    )

    assert receipt.layer == AckLayer.ACCEPTED
    assert receipt.notify_user is True
    assert "/deploy" in (receipt.message or "")
    assert pushed == [], "blocked command must not be pushed to the orchestrator"


# -- explicit orchestrator_ready hook (no monkey-patching) ---------------


@pytest.mark.asyncio
async def test_subsystem_run_fires_orchestrator_ready_before_polling() -> None:
    """OrchestrationSubsystem.run() invokes the ready hook after constructing
    the orchestrator and before its run() starts."""
    from orchestratord.orchestration_subsystem import OrchestrationSubsystem

    events: list[str] = []
    ready_args: list[object] = []

    class _FakeOrchestrator:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def run(self) -> None:
            events.append("orchestrator_run")

    def _ready(orch) -> None:
        ready_args.append(orch)
        events.append("ready")

    subsystem = OrchestrationSubsystem.__new__(OrchestrationSubsystem)
    subsystem.workflow = object()
    subsystem.tracker_adapter = object()
    subsystem.workspace_manager = object()
    subsystem.agent_runner = object()
    subsystem._backend = object()
    subsystem.status_dashboard = object()
    subsystem.stage_runners = {}
    subsystem._workflow_yaml_path = None
    subsystem._clarifier_provider_factory = None
    subsystem.orchestrator_ready = _ready

    import orchestratord.orchestrator as orchestrator_module

    original = orchestrator_module.Orchestrator
    orchestrator_module.Orchestrator = _FakeOrchestrator
    try:
        await subsystem.run()
    finally:
        orchestrator_module.Orchestrator = original

    assert events == ["ready", "orchestrator_run"]
    assert len(ready_args) == 1
    assert isinstance(ready_args[0], _FakeOrchestrator)
    assert subsystem._orchestrator is ready_args[0]


def _mount_fake_gateway(monkeypatch, subsystem):
    """Mount _mount_gateway_opt_in against fakes; return (wrapper, ipc)."""
    from orchestratord.cli import server as server_mod

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def reconnect_until_registered(self, **_kwargs):
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            return SimpleNamespace(ack_layer="accepted")

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin

        async def send_outbound(self, text):
            return None

    monkeypatch.setattr("orchestratord.ipc.client.GatewayIpcClient", _FakeIpc)
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient", _FakeClient
    )
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )
    return wrapper


class _FakeReadyOrch:
    """Records _emit_im_event calls (stands in for a constructed Orchestrator)."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, str, str]] = []

    def _emit_im_event(self, issue_id, event_type, level, message, payload=None):
        self.emitted.append((issue_id, event_type, message))


@pytest.mark.asyncio
async def test_mount_gateway_uses_ready_hook_without_class_patch(monkeypatch) -> None:
    """_mount_gateway_opt_in sets subsystem.orchestrator_ready and leaves
    subsystem.run / Orchestrator.run untouched; the hook injects the IM
    runtime attributes and emits the first event."""
    import orchestratord.orchestrator as orchestrator_module
    from orchestratord.orchestrator import Orchestrator

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    before_subsystem_run = subsystem.run
    before_class_run = Orchestrator.run

    wrapper = _mount_fake_gateway(monkeypatch, subsystem)

    assert wrapper is not None
    # No monkey-patching: the explicit hook is the only wiring.
    assert subsystem.run is before_subsystem_run
    assert Orchestrator.run is before_class_run
    assert orchestrator_module.Orchestrator.run is before_class_run
    assert callable(subsystem.orchestrator_ready)

    orch = _FakeReadyOrch()
    subsystem.orchestrator_ready(orch)

    assert orch._im_gateway_wrapper is wrapper
    assert isinstance(orch._im_gateway_session_id, str)
    assert orch._im_gateway_session_id.startswith("orchestrator-")
    assert callable(orch.im_event_deliver)
    assert orch.im_event_channel == "wechat"
    assert orch.emitted == [("", "orchestrator.started", "IM notifications enabled")]


@pytest.mark.asyncio
async def test_mount_gateway_ready_hook_propagates_feishu_adapter(monkeypatch) -> None:
    """The ready hook forwards a FeishuAppChannelAdapter for per-session
    activity sinks (no-op when absent)."""
    from orchestratord.cli import server as server_mod

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    feishu_adapter = SimpleNamespace(marker="feishu")

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.on_deliver = None

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client

    monkeypatch.setattr("orchestratord.ipc.client.GatewayIpcClient", _FakeIpc)
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient", _FakeClient
    )
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        SimpleNamespace(workspace=SimpleNamespace(root="/repo")),
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
        feishu_adapter=feishu_adapter,
    )

    orch = _FakeReadyOrch()
    subsystem.orchestrator_ready(orch)
    assert orch.im_channel_adapter is feishu_adapter

    # Without an adapter the attribute stays untouched.
    subsystem2 = SimpleNamespace(_orchestrator=None, run=_run)
    server_mod._mount_gateway_opt_in(
        subsystem2,
        SimpleNamespace(workspace=SimpleNamespace(root="/repo")),
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )
    plain_orch = _FakeReadyOrch()
    subsystem2.orchestrator_ready(plain_orch)
    assert not hasattr(plain_orch, "im_channel_adapter")
    assert wrapper is not None


@pytest.mark.asyncio
async def test_mount_gateway_two_instances_do_not_pollute(monkeypatch) -> None:
    """Two mounts inject only their own wrapper; an untouched orchestrator
    instance is not affected (the old class-level patch leaked here)."""
    from orchestratord.orchestrator import Orchestrator

    before_class_run = Orchestrator.run

    async def _run():
        return None

    subsystem_a = SimpleNamespace(_orchestrator=None, run=_run)
    subsystem_b = SimpleNamespace(_orchestrator=None, run=_run)
    wrapper_a = _mount_fake_gateway(monkeypatch, subsystem_a)
    wrapper_b = _mount_fake_gateway(monkeypatch, subsystem_b)
    assert wrapper_a is not wrapper_b

    orch_a, orch_b = _FakeReadyOrch(), _FakeReadyOrch()
    subsystem_a.orchestrator_ready(orch_a)
    subsystem_b.orchestrator_ready(orch_b)

    assert orch_a._im_gateway_wrapper is wrapper_a
    assert orch_b._im_gateway_wrapper is wrapper_b
    assert orch_a._im_gateway_wrapper is not wrapper_b
    # A third orchestrator constructed outside any mount stays clean —
    # under the former class-level Orchestrator.run patch, *every* instance
    # received the injection when it started.
    untouched = _FakeReadyOrch()
    assert not hasattr(untouched, "_im_gateway_wrapper")
    assert Orchestrator.run is before_class_run


# -- read-loop flush fix --------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_triggered_outbound_does_not_block_read_loop() -> None:
    """A DELIVER-triggered flush must not wait for its OUTBOUND ACK inside
    the on_deliver callback — the ACK can only be read by the IPC read loop
    that is currently blocked in this very callback (deadlock until
    reply_timeout)."""

    class _BlockingIpc:
        def __init__(self) -> None:
            self.on_deliver = None
            self.sent: list[str] = []
            self.complete_calls: list[tuple] = []
            self.release = asyncio.Event()

        async def send_outbound(self, *, origin, text, metadata=None, in_reply_to=None):
            self.sent.append(text)
            await self.release.wait()  # ACK never arrives while held

        async def complete_processing(self, *, message_id, outcome, reason):
            self.complete_calls.append((message_id, outcome, reason))

    ipc = _BlockingIpc()
    client = OrchestratorGatewayClient(
        _noop_handlers(), ipc_client=ipc, origin="im:direct:*:*"
    )
    client._queue_pending_outbound("queued reply")

    frame = GatewayFrame.deliver(
        delivery_id="d-block-1",
        session_id="orch",
        origin="im:direct:acct:user",
        text="hello",
        semantic="followUp",
    )
    # With the old inline `await self._flush_pending_outbound(force=True)`
    # this await hung on the never-released ACK. It must now return.
    await asyncio.wait_for(client._on_pushed_deliver(frame), timeout=1.0)

    # The delivery completes reliably even while the flush is in flight…
    assert ipc.complete_calls == [("d-block-1", "success", "followup_queued")]
    # …and the flush send started but holds no callback hostage.
    assert ipc.sent == ["queued reply"]
    assert list(client._pending_outbound) == ["queued reply"]

    ipc.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert list(client._pending_outbound) == []


@pytest.mark.asyncio
async def test_deliver_flush_serializes_consecutive_deliveries() -> None:
    """Consecutive DELIVERs reuse the running flush task instead of piling
    up one task per delivery."""
    ipc = _RecordingIpc()
    client = OrchestratorGatewayClient(
        _noop_handlers(), ipc_client=ipc, origin="im:direct:*:*"
    )

    async def _deliver(delivery_id: str) -> None:
        await client._on_pushed_deliver(
            GatewayFrame.deliver(
                delivery_id=delivery_id,
                session_id="orch",
                origin="im:direct:acct:user",
                text="hello",
            )
        )

    await _deliver("d-1")
    first_task = client._deliver_flush_task
    # The empty-queue flush task has completed; a second DELIVER may reuse
    # the slot. Neither delivery may leave a *pending* task behind.
    if first_task is not None:
        assert first_task.done()
    await _deliver("d-2")
    if client._deliver_flush_task is not None:
        assert client._deliver_flush_task.done()


# -- outbound metadata envelope + in_reply_to -----------------------------


@pytest.mark.asyncio
async def test_outbound_event_metadata_contains_issue_event_level_markdown() -> None:
    """Orchestrator events sent via build_ipc_deliver carry the SPEC Phase 4
    metadata envelope."""
    ipc = _RecordingIpc()
    client = OrchestratorGatewayClient(
        _noop_handlers(), ipc_client=ipc, origin="im:direct:*:*"
    )
    deliver = build_ipc_deliver(client)

    event = OrchestratorEvent(
        "issue.completed", "AGENTSDK-15", EventLevel.SUCCESS, "任务完成", {"pr": 15}
    )
    deliver(event, "✅ AGENTSDK-15: 任务完成")
    await asyncio.sleep(0.05)

    assert len(ipc.sent) == 1
    sent = ipc.sent[0]
    assert sent["origin"] == "im:direct:*:*"
    assert sent["text"].startswith("✅")
    metadata = sent["metadata"]
    assert metadata is not None
    assert metadata["issue_id"] == "AGENTSDK-15"
    assert metadata["event_type"] == "issue.completed"
    assert metadata["level"] == "success"
    assert metadata["markdown"] is True


@pytest.mark.asyncio
async def test_command_reply_threads_in_reply_to_delivery_id() -> None:
    """Command replies are threaded back to the triggering IM message via
    in_reply_to."""
    ipc = _RecordingIpc()

    def _run_cli(argv):
        return 0, "ISSUE-1 done", ""

    client = OrchestratorGatewayClient(
        _noop_handlers(),
        ipc_client=ipc,
        origin="im:direct:*:*",
        cli_runner=_run_cli,
    )

    await client._on_pushed_deliver(
        GatewayFrame.deliver(
            delivery_id="d-cmd-1",
            session_id="orch",
            origin="im:direct:acct:user",
            text="/issue list",
            semantic="command",
        )
    )

    assert len(ipc.sent) == 1
    assert ipc.sent[0]["origin"] == "im:direct:acct:user"
    assert ipc.sent[0]["in_reply_to"] == "d-cmd-1"
    assert "命令已执行" in ipc.sent[0]["text"]
    assert "ISSUE-1 done" in ipc.sent[0]["text"]


@pytest.mark.asyncio
async def test_send_outbound_forwards_explicit_metadata_and_in_reply_to() -> None:
    """Explicit send_outbound kwargs reach the IPC client unchanged."""
    ipc = _RecordingIpc()
    client = OrchestratorGatewayClient(
        _noop_handlers(), ipc_client=ipc, origin="im:direct:*:*"
    )

    await client.send_outbound(
        "note",
        metadata={"issue_id": "I1", "event_type": "note", "level": "info"},
        in_reply_to="d-9",
    )

    assert len(ipc.sent) == 1
    assert ipc.sent[0]["metadata"]["issue_id"] == "I1"
    assert ipc.sent[0]["in_reply_to"] == "d-9"


def test_pending_replies_with_same_text_keep_distinct_origins() -> None:
    client = OrchestratorGatewayClient(_noop_handlers(), origin="im:direct:*:*")

    client._queue_pending_outbound(
        "same reply",
        in_reply_to="d-feishu",
        origin="feishu:dm:app:ou_operator",
    )
    client._queue_pending_outbound(
        "same reply",
        in_reply_to="d-wechat",
        origin="wechat:direct:default:wx_operator",
    )

    assert list(client._pending_outbound) == ["same reply", "same reply"]
    assert [extra["origin"] for extra in client._pending_outbound_extras] == [
        "feishu:dm:app:ou_operator",
        "wechat:direct:default:wx_operator",
    ]
