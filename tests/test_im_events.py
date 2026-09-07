"""Tests for the orchestrator → IM event bridge (P3)."""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestratord.events import (
    EventLevel,
    OrchestratorEvent,
    OrchestratorEventEmitter,
    format_event,
)
from orchestratord.ipc import IM_DIRECT_ALL_ORIGIN
from orchestratord.sinks.channel import (
    ChannelProgressSink,
    build_gateway_deliver,
)


def _session(issue_id="AGENTSDK-15", reason="success", pr=None):
    issue = SimpleNamespace(id=issue_id, identifier=issue_id, pr_url=pr)
    return SimpleNamespace(issue=issue, status="running", turn_count=3)


def _handlers(**overrides):
    from orchestratord.im_gateway_client import OrchestratorHandlers

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


# -- formatter ---------------------------------------------------------


def test_formatter_success_terminal():
    e = OrchestratorEvent(
        "issue.completed", "AGENTSDK-15", EventLevel.SUCCESS, "任务完成", {"pr": 15}
    )
    assert "✅" in format_event(e)
    assert "AGENTSDK-15" in format_event(e)
    assert "PR 15" in format_event(e)


def test_formatter_error_events():
    e = OrchestratorEvent("post_commit_failed", "I1", EventLevel.ERROR, "boom", {"pr": 9})
    txt = format_event(e)
    assert txt.startswith("❌")
    assert "需人工介入" in txt


def test_formatter_unknown_fallback():
    e = OrchestratorEvent("something.weird", "I1", EventLevel.WARN, "x")
    assert format_event(e) == "⚠️ I1: x"


def test_formatter_issue_detected_with_url():
    """issue.detected includes ISSUE- prefixed title, repo, and URL."""
    e = OrchestratorEvent(
        "issue.detected",
        "AGENTSDK-15",
        EventLevel.INFO,
        "新增 ISSUE",
        {
            "title": "Fix login bug",
            "repo": "owner/repo",
            "url": "https://gitcode.com/owner/repo/issues/15",
        },
    )
    txt = format_event(e)
    assert "ℹ️" in txt
    assert "AGENTSDK-15" in txt
    assert "新增 ISSUE" in txt
    assert "ISSUE-Fix login bug" in txt
    assert "owner/repo" in txt
    assert "https://gitcode.com/owner/repo/issues/15" in txt


def test_formatter_issue_detected_without_url():
    """issue.detected without URL still renders cleanly (no trailing dot)."""
    e = OrchestratorEvent(
        "issue.detected",
        "I1",
        EventLevel.INFO,
        "新增 ISSUE",
        {"title": "Some task"},
    )
    txt = format_event(e)
    assert "新增 ISSUE" in txt
    assert "ISSUE-Some task" in txt
    assert not txt.rstrip().endswith("·")


def test_formatter_issue_started_with_rich_payload():
    """issue.started includes title, branch, repo from payload."""
    e = OrchestratorEvent(
        "issue.started",
        "AGENTSDK-15",
        EventLevel.INFO,
        "任务已启动",
        {"title": "Fix login bug", "branch": "orchestratord/AGENTSDK-15", "repo": "owner/repo"},
    )
    txt = format_event(e)
    assert "AGENTSDK-15" in txt
    assert "ISSUE-Fix login bug" in txt
    assert "orchestratord/AGENTSDK-15" in txt
    assert "owner/repo" in txt


def test_formatter_issue_completed_with_verification_and_commit():
    """issue.completed includes verification status, commit, branch."""
    e = OrchestratorEvent(
        "issue.completed",
        "AGENTSDK-15",
        EventLevel.SUCCESS,
        "任务完成",
        {
            "title": "Fix login bug",
            "branch": "orchestratord/AGENTSDK-15",
            "verification": "passed",
            "commit": "abc1234567",
            "pr": "https://gitcode.com/owner/repo/pulls/15",
        },
    )
    txt = format_event(e)
    assert "✅" in txt
    assert "任务完成" in txt
    assert "ISSUE-Fix login bug" in txt
    assert "orchestratord/AGENTSDK-15" in txt
    assert "passed" in txt
    assert "abc1234" in txt  # commit truncated to 7 chars
    assert "PR" in txt


def test_formatter_issue_failed_with_attempts():
    """issue.failed includes attempts and turns."""
    e = OrchestratorEvent(
        "issue.failed",
        "AGENTSDK-15",
        EventLevel.WARN,
        "Agent run timed out",
        {"title": "Fix login bug", "branch": "orchestratord/AGENTSDK-15", "attempts": 2, "turns": 42},
    )
    txt = format_event(e)
    assert "⚠️" in txt
    assert "Agent run timed out" in txt
    assert "第 2 次尝试" in txt
    assert "42 轮" in txt


def test_formatter_pr_opened_with_branch_and_commit():
    """pr.opened includes branch and commit."""
    e = OrchestratorEvent(
        "pr.opened",
        "AGENTSDK-15",
        EventLevel.INFO,
        "PR opened",
        {
            "branch": "orchestratord/AGENTSDK-15",
            "commit": "deadbeef",
            "pr": "https://gitcode.com/owner/repo/pulls/15",
        },
    )
    txt = format_event(e)
    assert "PR 已开启" in txt
    assert "orchestratord/AGENTSDK-15" in txt
    assert "deadbee" in txt  # truncated


def test_formatter_verification_failed_with_branch():
    """verification.failed includes branch."""
    e = OrchestratorEvent(
        "verification.failed",
        "AGENTSDK-15",
        EventLevel.WARN,
        "pytest failed",
        {"branch": "orchestratord/AGENTSDK-15", "commit": "abc1234"},
    )
    txt = format_event(e)
    assert "验证失败" in txt
    assert "orchestratord/AGENTSDK-15" in txt
    assert "abc1234" in txt


def test_formatter_post_commit_failed_with_commit():
    """post_commit_failed includes commit and branch."""
    e = OrchestratorEvent(
        "post_commit_failed",
        "AGENTSDK-15",
        EventLevel.ERROR,
        "push error",
        {"branch": "orchestratord/AGENTSDK-15", "commit": "abc12345", "pr": 15},
    )
    txt = format_event(e)
    assert "需人工介入" in txt
    assert "orchestratord/AGENTSDK-15" in txt
    assert "abc1234" in txt


def test_formatter_empty_payload_still_works():
    """Events with empty payload produce clean output without trailing dots."""
    e = OrchestratorEvent("issue.started", "I1", EventLevel.INFO, "任务已启动", {})
    txt = format_event(e)
    assert "I1" in txt
    assert "任务已启动" in txt
    # No trailing " · " when payload is empty
    assert not txt.rstrip().endswith("·")


def test_formatter_agent_stagnation_with_turns():
    """agent.stagnation includes turn count."""
    e = OrchestratorEvent(
        "agent.stagnation", "I1", EventLevel.WARN, "agent 长时间无进展", {"turns": 100}
    )
    txt = format_event(e)
    assert "100 轮" in txt


# -- emitter -----------------------------------------------------------


def test_emitter_session_success_waits_for_orchestrator_finalization():
    received = []
    em = OrchestratorEventEmitter("AGENTSDK-15", sinks=[received.append])
    em.on_session_complete(SimpleNamespace(reason="success"), _session(pr="https://x/p/15"))
    assert received == []


@pytest.mark.parametrize("reason", ["task_complete", "already_completed"])
def test_emitter_session_completion_aliases_wait_for_orchestrator(reason):
    received = []
    em = OrchestratorEventEmitter("AGENTSDK-15", sinks=[received.append])
    em.on_session_complete(SimpleNamespace(reason=reason), _session(pr="https://x/p/15"))
    assert received == []


def test_emitter_session_rate_limit_emits_error():
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])
    em.on_session_complete(SimpleNamespace(reason="rate_limit_circuit_open"), _session())
    assert received[0].event_type == "agent.rate_limit_circuit_open"
    assert received[0].level is EventLevel.ERROR


def test_emitter_session_failure_skipped_for_status_branch_reasons():
    # stagnation / loop_detected / budget_exhausted / max_turns_exceeded
    # are owned by the orchestrator's status dispatch (agent.*); the sink
    # callback must not also emit a generic issue.failed (would double).
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])
    em.on_session_complete(SimpleNamespace(reason="stagnation"), _session())
    assert received == []
    em.on_session_complete(SimpleNamespace(reason="loop_detected"), _session())
    assert received == []
    em.on_session_complete(SimpleNamespace(reason="budget_exhausted"), _session())
    assert received == []


def test_emitter_explicit_emit_reaches_sink():
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])
    em.emit(OrchestratorEvent("clarification.notify_emitted", "I1", EventLevel.WARN, "need input"))
    assert received[0].event_type == "clarification.notify_emitted"


def test_emitter_does_not_suppress_repeated_warn_events():
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])
    ev = OrchestratorEvent("agent.stagnation", "I1", EventLevel.WARN, "stuck")
    em.emit(ev)
    em.emit(ev)

    assert [event.event_type for event in received] == ["agent.stagnation", "agent.stagnation"]


def test_emitter_info_events_are_dispatched_immediately():
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])
    ev = OrchestratorEvent("intent.followup", "I1", EventLevel.INFO, "q")
    em.emit(ev)
    em.emit(ev)

    assert [event.event_type for event in received] == ["intent.followup", "intent.followup"]
    em.flush("I1")
    assert [event.event_type for event in received] == ["intent.followup", "intent.followup"]


@pytest.mark.asyncio
async def test_emitter_info_events_do_not_auto_aggregate():
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])

    em.emit(OrchestratorEvent("intent.followup", "I1", EventLevel.INFO, "q"))
    em.emit(OrchestratorEvent("intent.hint", "I1", EventLevel.INFO, "h"))
    await asyncio.sleep(0.03)

    assert [event.event_type for event in received] == ["intent.followup", "intent.hint"]


def test_emitter_immediate_info_events_are_not_buffered():
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])

    em.emit(OrchestratorEvent("issue.started", "I1", EventLevel.INFO, "started"))

    assert [event.event_type for event in received] == ["issue.started"]


def test_emitter_exception_isolation():
    good = []

    def bad_sink(e):
        raise RuntimeError("boom")

    em = OrchestratorEventEmitter("I1", sinks=[bad_sink, good.append])
    em.emit(OrchestratorEvent("issue.failed", "I1", EventLevel.ERROR, "x"))
    assert len(good) == 1  # bad sink did not block the good one


def test_emitter_phase_and_turn_are_noop_for_im():
    received = []
    em = OrchestratorEventEmitter("I1", sinks=[received.append])
    em.on_phase_complete(SimpleNamespace(phase=1, turn_count=2), _session())
    em.on_turn_complete(SimpleNamespace(turn=2), _session())
    assert received == []


# -- ChannelProgressSink ----------------------------------------------


def test_channel_sink_delivers_formatted_text():
    delivered = []
    sink = ChannelProgressSink(lambda e, txt: delivered.append((e.event_type, txt)))
    sink(OrchestratorEvent("issue.completed", "I1", EventLevel.SUCCESS, "任务完成", {"pr": 7}))
    assert delivered[0][0] == "issue.completed"
    assert "✅" in delivered[0][1]
    assert "PR 7" in delivered[0][1]
    assert len(sink.events) == 1


def test_channel_sink_deliver_exception_isolated():
    sink = ChannelProgressSink(lambda e, txt: (_ for _ in ()).throw(RuntimeError("x")))
    # must not raise
    sink(OrchestratorEvent("issue.failed", "I1", EventLevel.ERROR, "x"))
    assert len(sink.events) == 1  # event still recorded


# -- gateway deliverer (fake gateway) ---------------------------------


@pytest.mark.asyncio
async def test_build_gateway_deliver_schedules_send():
    class _FakeGateway:
        def __init__(self):
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)

    gw = _FakeGateway()
    loop = asyncio.get_event_loop()
    deliver = build_gateway_deliver(gw, "wechat-main", loop=loop)
    deliver(OrchestratorEvent("issue.failed", "I1", EventLevel.ERROR, "boom"), "❌ I1: boom")
    # let the scheduled task run
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(gw.sent) == 1
    assert gw.sent[0].channel == "wechat-main"
    assert gw.sent[0].level == "error"
    assert "I1" in gw.sent[0].text


def test_build_gateway_deliver_drops_without_loop():
    class _FakeGateway:
        async def send(self, msg):
            pass

    # No running loop in a sync test context here either; deliver must not raise.
    deliver = build_gateway_deliver(_FakeGateway(), "wechat-main")
    # Calling outside a loop logs + drops; we only assert no raise.
    deliver(OrchestratorEvent("issue.failed", "I1", EventLevel.ERROR, "x"), "txt")


def test_run_orchestrator_starts_im_heartbeat_inside_runtime_loop(monkeypatch, tmp_path) -> None:
    """P6 opt-in must run on the asyncio.run() loop, not an inactive old loop."""
    from types import SimpleNamespace

    from orchestratord.cli import server as server_mod

    events: list[str] = []

    class _OldLoop:
        def add_signal_handler(self, *args, **kwargs):
            return None

        def create_task(self, coro):
            events.append("old_loop_task")
            coro.close()

    class _FakeWorkflowLoader:
        @staticmethod
        def load(_workflow_path):
            cfg = SimpleNamespace(
                tracker=SimpleNamespace(),
                workspace=SimpleNamespace(root=str(tmp_path)),
            )
            return cfg, "prompt"

    class _FakeWorkflowStore:
        def load(self, _workflow_path):
            return None

    class _FakeSubsystem:
        def __init__(self, _config, **_kwargs):
            self.status_dashboard = SimpleNamespace(state=dict)

        async def run(self):
            events.append("subsystem_run")
            await asyncio.sleep(0)

        async def shutdown(self):
            return None

    class _FakeImWrapper:
        async def _heartbeat_loop(self):
            events.append("heartbeat")

    monkeypatch.setattr(server_mod.asyncio, "get_event_loop", lambda: _OldLoop())
    monkeypatch.setattr(
        "orchestratord.workflow.WorkflowLoader",
        _FakeWorkflowLoader,
    )
    monkeypatch.setattr(
        "orchestratord.tracker.validate_tracker_config",
        lambda _cfg: None,
    )
    monkeypatch.setattr(
        "orchestratord.workflow_store.get_workflow_store",
        lambda: _FakeWorkflowStore(),
    )
    # P5（DESIGN §6 :431）：_run_orchestrator 经 applications 注册表解析
    # 组合根类——桩子类须打在注册表 seam 上（旧的
    # orchestration_subsystem.OrchestrationSubsystem 基类 patch 依赖
    # app.py 尚未 import 的类体冻结时序，注册表寻址后不再成立）。
    monkeypatch.setattr(
        "orchestratord.applications.get_application_class",
        lambda _name: _FakeSubsystem,
    )
    monkeypatch.setattr(
        server_mod,
        "_mount_gateway_opt_in",
        lambda _subsystem, _config, **_kwargs: _FakeImWrapper(),
    )

    rc = server_mod._run_orchestrator(str(tmp_path / "WORKFLOW.md"))

    assert rc == 0
    assert "subsystem_run" in events
    assert "heartbeat" in events
    assert "old_loop_task" not in events


def test_orchestrator_connect_gateway_reports_not_started(monkeypatch, capsys) -> None:
    """Dynamic connect should fail clearly when no orchestrator daemon is live."""
    from orchestratord.cli import server as server_mod

    args = SimpleNamespace(
        workspace=None,
        workflow=None,
        gateway=None,
        gateway_sock=None,
    )
    monkeypatch.setattr(server_mod, "_find_metadata", lambda _args: (None, None))

    rc = server_mod._run_connect_gateway(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "连接失败，orchestrator未启动" in captured.err


def test_orchestrator_connect_gateway_gateway_option_reports_not_started(
    monkeypatch, capsys
) -> None:
    """--gateway ORIGIN still checks the daemon before submitting a control request."""
    from orchestratord.cli import server as server_mod

    args = SimpleNamespace(
        workspace=None,
        workflow=None,
        gateway="wechat:direct:default:user",
        gateway_sock=None,
    )
    monkeypatch.setattr(server_mod, "_find_metadata", lambda _args: (None, None))

    rc = server_mod._run_connect_gateway(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "连接失败，orchestrator未启动" in captured.err


def test_orchestrator_connect_gateway_parser_defaults_without_gateway_origin(capsys) -> None:
    """connect-gateway has no --gateway-origin and defaults to all direct/private messages."""
    from orchestratord.cli import server as server_mod

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    server_mod.add_server_parser(subparsers)

    args = parser.parse_args(["server", "connect-gateway"])
    assert args.gateway is None

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "server",
                "connect-gateway",
                "--gateway-origin",
                "wechat:direct:default:user",
            ]
        )
    capsys.readouterr()


def test_orchestrator_connect_gateway_writes_control_file(monkeypatch, capsys, tmp_path) -> None:
    """Running daemons receive a gateway_connect control file for the next poll."""
    from orchestratord.cli import server as server_mod

    args = SimpleNamespace(
        workspace=None,
        workflow=None,
        gateway=None,
        gateway_sock="/tmp/gateway.sock",
    )
    meta = {"pid": 12345, "workspace_root": str(tmp_path)}
    monkeypatch.setattr(server_mod, "_find_metadata", lambda _args: (None, meta))
    monkeypatch.setattr(server_mod, "_is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(server_mod, "_gateway_socket_available", lambda _sock: True)
    monkeypatch.setattr(server_mod, "_wait_gateway_control_result", lambda *_args, **_kwargs: None)

    rc = server_mod._run_connect_gateway(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert "gateway connect request submitted" in captured.out
    control_files = list((tmp_path / ".orchestrator_control").glob("gateway_connect_*.control"))
    assert len(control_files) == 1
    cmd, issue_id, extra = control_files[0].read_text(encoding="utf-8").splitlines()
    assert cmd == "gateway_connect"
    assert issue_id == ""
    payload = json.loads(extra)
    assert payload["origin"] == IM_DIRECT_ALL_ORIGIN
    assert payload["sock"] == "/tmp/gateway.sock"
    assert payload["response_path"].endswith(".result.json")


def test_orchestrator_connect_gateway_accepts_specific_origin(monkeypatch, tmp_path) -> None:
    """--gateway ORIGIN requests a specific runtime binding instead of the default."""
    from orchestratord.cli import server as server_mod

    args = SimpleNamespace(
        workspace=None,
        workflow=None,
        gateway="wechat:direct:default:user",
        gateway_sock="/tmp/gateway.sock",
    )
    meta = {"pid": 12345, "workspace_root": str(tmp_path)}
    monkeypatch.setattr(server_mod, "_find_metadata", lambda _args: (None, meta))
    monkeypatch.setattr(server_mod, "_is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(server_mod, "_gateway_socket_available", lambda _sock: True)
    monkeypatch.setattr(server_mod, "_wait_gateway_control_result", lambda *_args, **_kwargs: None)

    rc = server_mod._run_connect_gateway(args)

    assert rc == 0
    control_files = list((tmp_path / ".orchestrator_control").glob("gateway_connect_*.control"))
    assert len(control_files) == 1
    _cmd, _issue_id, extra = control_files[0].read_text(encoding="utf-8").splitlines()
    assert json.loads(extra)["origin"] == "wechat:direct:default:user"


def test_orchestrator_connect_gateway_missing_gateway_fails(monkeypatch, capsys, tmp_path) -> None:
    """Gateway socket absence should fail before writing a control request."""
    from orchestratord.cli import server as server_mod

    args = SimpleNamespace(
        workspace=None,
        workflow=None,
        gateway=None,
        gateway_sock="/tmp/missing-gateway.sock",
    )
    meta = {"pid": 12345, "workspace_root": str(tmp_path)}
    monkeypatch.setattr(server_mod, "_find_metadata", lambda _args: (None, meta))
    monkeypatch.setattr(server_mod, "_is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(server_mod, "_gateway_socket_available", lambda _sock: False)

    rc = server_mod._run_connect_gateway(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert "IM gateway daemon is not running" in captured.err
    assert not (tmp_path / ".orchestrator_control").exists()


def test_orchestrator_disconnect_gateway_writes_control_file(monkeypatch, capsys, tmp_path) -> None:
    """disconnect-gateway reuses the same orchestrator control directory."""
    from orchestratord.cli import server as server_mod

    args = SimpleNamespace(workspace=None, workflow=None)
    meta = {"pid": 12345, "workspace_root": str(tmp_path)}
    monkeypatch.setattr(server_mod, "_find_metadata", lambda _args: (None, meta))
    monkeypatch.setattr(server_mod, "_is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(server_mod, "_wait_gateway_control_result", lambda *_args, **_kwargs: None)

    rc = server_mod._run_disconnect_gateway(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert "gateway disconnect request submitted" in captured.out
    control_files = list((tmp_path / ".orchestrator_control").glob("gateway_disconnect_*.control"))
    assert len(control_files) == 1
    cmd, issue_id, extra = control_files[0].read_text(encoding="utf-8").splitlines()
    assert cmd == "gateway_disconnect"
    assert issue_id == ""
    payload = json.loads(extra)
    assert payload["response_path"].endswith(".result.json")


@pytest.mark.asyncio
async def test_orchestrator_control_poll_connects_and_disconnects_gateway(
    monkeypatch, tmp_path
) -> None:
    """The daemon handles gateway control files inside the running process."""
    from orchestratord.orchestrator import Orchestrator

    calls: list[tuple[str, object]] = []

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def connect(self):
            calls.append(("connect", self.sock))

        async def register(self, *, session_id, origin, capabilities):
            calls.append(("register", (session_id, origin, tuple(capabilities))))
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            return SimpleNamespace(ack_layer="accepted")

        async def unregister(self, session_id=None):
            calls.append(("unregister", session_id))
            return SimpleNamespace(ack_layer="accepted")

        async def close(self):
            calls.append(("close", self.sock))

    class _FakeWrapper:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin
            self.sent: list[str] = []

        async def send_outbound(self, text):
            self.sent.append(text)

        async def _flush_pending_outbound(self):
            calls.append(("flush", self._origin))

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeWrapper,
    )

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._workspace_root = tmp_path
    orchestrator._im_emitters = {}
    orchestrator.im_event_deliver = None
    orchestrator.im_event_channel = ""
    # 局部构造绕过 __init__，补绑 C2b 应用侧协作对象。
    from orchestratord.applications.issue_pr.lifecycle import IssueToPrLifecycle

    orchestrator._issue_app = IssueToPrLifecycle(orchestrator)

    control_dir = tmp_path / ".orchestrator_control"
    control_dir.mkdir()
    connect_result = control_dir / "gateway-connect.result.json"
    connect_payload = {
        "origin": IM_DIRECT_ALL_ORIGIN,
        "sock": "/tmp/gateway.sock",
        "response_path": str(connect_result),
    }
    (control_dir / "gateway_connect_1.control").write_text(
        "gateway_connect\n\n" + json.dumps(connect_payload),
        encoding="utf-8",
    )

    await orchestrator._process_control_commands()

    assert ("connect", "/tmp/gateway.sock") in calls
    assert any(call[0] == "register" and call[1][1] == IM_DIRECT_ALL_ORIGIN for call in calls)
    assert getattr(orchestrator, "_im_gateway_wrapper", None) is not None
    assert callable(orchestrator.im_event_deliver)
    assert json.loads(connect_result.read_text(encoding="utf-8"))["ok"] is True

    disconnect_result = control_dir / "gateway-disconnect.result.json"
    disconnect_payload = {"response_path": str(disconnect_result)}
    (control_dir / "gateway_disconnect_1.control").write_text(
        "gateway_disconnect\n\n" + json.dumps(disconnect_payload),
        encoding="utf-8",
    )

    await orchestrator._process_control_commands()

    assert any(call[0] == "unregister" for call in calls)
    assert any(call[0] == "close" for call in calls)
    assert getattr(orchestrator, "_im_gateway_wrapper", None) is None
    assert orchestrator.im_event_deliver is None
    assert json.loads(disconnect_result.read_text(encoding="utf-8"))["ok"] is True


def test_mount_gateway_switch_uses_all_private_origin(monkeypatch) -> None:
    """Startup opt-in can bind all supported private IM messages without origin details."""
    from orchestratord.cli import server as server_mod

    created: dict[str, object] = {}

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def connect(self):
            return None

        async def register(self, **kwargs):
            return None

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            created["origin"] = origin
            created["ipc"] = ipc_client

        async def _heartbeat_loop(self, interval=30.0):
            return None

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeClient,
    )

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )

    assert wrapper is not None
    assert created["origin"] == IM_DIRECT_ALL_ORIGIN


def test_mount_gateway_uses_reconnect_register(monkeypatch) -> None:
    """Startup opt-in must use the reconnect/register path, not a one-shot connect."""
    from orchestratord.cli import server as server_mod

    calls: list[tuple[str, str]] = []

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def reconnect_until_registered(self, *, session_id, origin, capabilities, **_kwargs):
            calls.append((session_id, origin))
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            return None

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin

        async def send_outbound(self, text):
            return None

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeClient,
    )

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )

    async def _drive_heartbeat_once():
        task = asyncio.create_task(wrapper._heartbeat_loop())
        await asyncio.sleep(0)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive_heartbeat_once())

    assert calls
    assert calls[0][1] == IM_DIRECT_ALL_ORIGIN


def test_mount_gateway_retries_initial_register_failure(monkeypatch) -> None:
    """Initial gateway unavailability should not permanently disable IM."""
    from orchestratord.cli import server as server_mod

    calls: list[str] = []

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def reconnect_until_registered(self, *, session_id, origin, capabilities, **_kwargs):
            calls.append(origin)
            if len(calls) == 1:
                return None
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            raise asyncio.CancelledError()

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin

        async def send_outbound(self, text):
            return None

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_delay):
        await _real_sleep(0)

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeClient,
    )
    monkeypatch.setattr(server_mod.asyncio, "sleep", _fast_sleep)

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )

    with __import__("contextlib").suppress(asyncio.CancelledError):
        asyncio.run(wrapper._heartbeat_loop())

    assert calls == [IM_DIRECT_ALL_ORIGIN, IM_DIRECT_ALL_ORIGIN]


def test_mount_gateway_reconnects_when_heartbeat_is_not_accepted(monkeypatch) -> None:
    """Two consecutive heartbeat timeouts should rebuild the registration."""
    from orchestratord.cli import server as server_mod

    reconnect_calls: list[str] = []
    heartbeat_calls = 0

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def reconnect_until_registered(self, *, session_id, origin, capabilities, **_kwargs):
            reconnect_calls.append(origin)
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            if heartbeat_calls <= 2:
                return
            raise asyncio.CancelledError()

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin

        async def send_outbound(self, text):
            return None

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_delay):
        await _real_sleep(0)

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeClient,
    )
    monkeypatch.setattr(server_mod.asyncio, "sleep", _fast_sleep)

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )

    with __import__("contextlib").suppress(asyncio.CancelledError):
        asyncio.run(wrapper._heartbeat_loop())

    assert reconnect_calls == [IM_DIRECT_ALL_ORIGIN, IM_DIRECT_ALL_ORIGIN]


def test_mount_gateway_keeps_registration_after_one_heartbeat_timeout(monkeypatch) -> None:
    """One delayed ACK can be provider head-of-line blocking, not a dead socket."""
    from orchestratord.cli import server as server_mod

    reconnect_calls: list[str] = []
    heartbeat_calls = 0

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def reconnect_until_registered(self, *, session_id, origin, capabilities, **_kwargs):
            reconnect_calls.append(origin)
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            if heartbeat_calls == 1:
                return None
            if heartbeat_calls == 2:
                return SimpleNamespace(ack_layer="accepted")
            raise asyncio.CancelledError()

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin

        async def send_outbound(self, text):
            return None

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_delay):
        await _real_sleep(0)

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeClient,
    )
    monkeypatch.setattr(server_mod.asyncio, "sleep", _fast_sleep)

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )

    with __import__("contextlib").suppress(asyncio.CancelledError):
        asyncio.run(wrapper._heartbeat_loop())

    assert reconnect_calls == [IM_DIRECT_ALL_ORIGIN]


def test_mount_gateway_flushes_pending_outbound_after_register(monkeypatch) -> None:
    """A successful register should flush queued events (e.g. the startup
    notification emitted before the socket was open). No reply-origin
    backfill is involved — the orchestrator reuses the wildcard OUTBOUND
    channel and the gateway resolves the sender."""
    from orchestratord.cli import server as server_mod

    calls: list[str] = []

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None

        async def reconnect_until_registered(self, *, session_id, origin, capabilities, **_kwargs):
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            raise asyncio.CancelledError()

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin

        async def _flush_pending_outbound(self):
            calls.append("flush")

        async def send_outbound(self, text):
            return None

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeClient,
    )

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )

    with __import__("contextlib").suppress(asyncio.CancelledError):
        asyncio.run(wrapper._heartbeat_loop())

    assert calls == ["flush"]


def test_mount_gateway_flushes_pending_outbound_after_accepted_heartbeat(monkeypatch) -> None:
    """Accepted heartbeats should retry queued outbound events without inbound traffic."""
    from orchestratord.cli import server as server_mod

    calls: list[str] = []

    class _FakeIpc:
        def __init__(self, sock, instance_id=None):
            self.sock = sock
            self.instance_id = instance_id
            self.on_deliver = None
            self.heartbeats = 0

        async def reconnect_until_registered(self, *, session_id, origin, capabilities, **_kwargs):
            return SimpleNamespace(ack_layer="accepted")

        async def heartbeat(self):
            self.heartbeats += 1
            if self.heartbeats >= 2:
                raise asyncio.CancelledError()
            return SimpleNamespace(ack_layer="accepted")

    class _FakeClient:
        def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
            self._ipc = ipc_client
            self._origin = origin

        async def _flush_pending_outbound(self):
            calls.append("flush")

        async def send_outbound(self, text):
            return None

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeIpc,
    )
    monkeypatch.setattr(
        "orchestratord.im_gateway_client.OrchestratorGatewayClient",
        _FakeClient,
    )

    async def _run():
        return None

    subsystem = SimpleNamespace(_orchestrator=None, run=_run)
    config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
    wrapper = server_mod._mount_gateway_opt_in(
        subsystem,
        config,
        enabled=True,
        origin=None,
        sock="/tmp/gateway.sock",
    )

    with __import__("contextlib").suppress(asyncio.CancelledError):
        asyncio.run(wrapper._heartbeat_loop())

    assert calls == ["flush", "flush"]


# -- opt-in inbound dispatch + IPC outbound (P6) --------------------------


@pytest.mark.asyncio
async def test_orchestrator_on_pushed_deliver_dispatches_control_verb() -> None:
    """A server-pushed DELIVER frame dispatches to the bound handlers."""
    from orchestratord.im_gateway_client import (
        OrchestratorGatewayClient,
    )
    from orchestratord.ipc import GatewayFrame

    control_calls: list[tuple] = []

    class _FakeIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[str] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append(text)

    ipc = _FakeIpc()
    handlers = _handlers(control_verb=lambda v, iid: control_calls.append((v, iid)))
    client = OrchestratorGatewayClient(handlers, ipc_client=ipc, origin="wechat:direct:a:u")
    assert ipc.on_deliver is not None  # wired on construct

    # /pause with an issue id → command → control_verb
    frame = GatewayFrame.deliver(
        delivery_id="d1",
        session_id="orch",
        origin="wechat:direct:a:u",
        metadata={"authenticated_origin": "wechat:direct:a:u"},
        text="/issue pause --id AGENTSDK-15",
        semantic="command",
    )
    await client._on_pushed_deliver(frame)
    assert control_calls and control_calls[0][0] == "pause"


@pytest.mark.parametrize(
    ("text", "argv"),
    [
        (
            "/issue feedback --id AGENTSDK-15 --approve",
            ["issue", "feedback", "--id", "AGENTSDK-15", "--approve"],
        ),
        (
            '/issue review --id AGENTSDK-15 --reject --feedback "needs tests"',
            [
                "issue",
                "review",
                "--id",
                "AGENTSDK-15",
                "--reject",
                "--feedback",
                "needs tests",
            ],
        ),
        (
            "/issue retry --id AGENTSDK-15 --mode reset",
            ["issue", "retry", "--id", "AGENTSDK-15", "--mode", "reset"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_orchestrator_on_pushed_deliver_dispatches_lifecycle_issue_cli(
    text: str, argv: list[str]
) -> None:
    """Lifecycle issue slash commands run through the existing orchestrator CLI path."""
    from orchestratord.im_gateway_client import OrchestratorGatewayClient
    from orchestratord.ipc import GatewayFrame

    cli_calls: list[list[str]] = []

    class _FakeIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[str] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append(text)
            return GatewayFrame.ack(delivery_id="d1", layer="processed", message="sent")

    def _run_cli(actual_argv: list[str]) -> tuple[int, str, str]:
        cli_calls.append(actual_argv)
        return 0, "ok", ""

    ipc = _FakeIpc()
    client = OrchestratorGatewayClient(
        _handlers(),
        ipc_client=ipc,
        origin="wechat:direct:a:u",
        cli_runner=_run_cli,
    )

    await client._on_pushed_deliver(
        GatewayFrame.deliver(
            delivery_id="d1",
            session_id="orch",
            origin="wechat:direct:a:u",
            metadata={"authenticated_origin": "wechat:direct:a:u"},
            text=text,
            semantic="command",
        )
    )

    assert cli_calls == [argv]
    assert ipc.sent
    assert ipc.sent[0].startswith(f"命令已执行：{text}")
    assert "ok" in ipc.sent[0]


@pytest.mark.asyncio
async def test_orchestrator_all_private_binding_sends_wildcard_after_inbound() -> None:
    """After an inbound DELIVER, orchestrator OUTBOUND still targets the wildcard
    origin — the gateway resolves it to the actual sender (the adapter's
    most-recent sender). The orchestrator does not track a concrete reply
    origin; it reuses the single OUTBOUND channel."""
    from orchestratord.im_gateway_client import (
        OrchestratorGatewayClient,
    )
    from orchestratord.ipc import GatewayFrame

    class _FakeIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[tuple[str, str]] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append((origin, text))

    ipc = _FakeIpc()
    handlers = _handlers()
    client = OrchestratorGatewayClient(handlers, ipc_client=ipc, origin="wechat:direct:*:*")
    await client._on_pushed_deliver(
        GatewayFrame.deliver(
            delivery_id="d1",
            session_id="orch",
            origin="wechat:direct:acct:user_sender",
            text="hello",
        )
    )
    await client.send_outbound("event text")

    assert ipc.sent == [("wechat:direct:*:*", "event text")]


@pytest.mark.asyncio
async def test_orchestrator_all_private_binding_sends_to_wildcard_then_wildcard() -> None:
    """Wildcard orchestrator binding always sends OUTBOUND to the wildcard
    (the gateway resolves it), before and after an inbound DELIVER — no
    concrete reply origin is tracked client-side."""
    from orchestratord.im_gateway_client import (
        OrchestratorGatewayClient,
    )
    from orchestratord.ipc.protocol import GatewayFrame

    class _FakeIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[tuple[str, str]] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append((origin, text))
            return GatewayFrame.ack(delivery_id="d", layer="processed", message="sent")

    ipc = _FakeIpc()
    handlers = _handlers()
    client = OrchestratorGatewayClient(handlers, ipc_client=ipc, origin="wechat:direct:*:*")
    # Before any inbound: emit to the wildcard — no premature queueing.
    await client.send_outbound("event text")
    assert ipc.sent == [("wechat:direct:*:*", "event text")]
    assert list(client._pending_outbound) == []

    # A concrete inbound arrives; the orchestrator does NOT switch to a
    # concrete reply origin — it keeps reusing the wildcard OUTBOUND channel.
    await client._on_pushed_deliver(
        GatewayFrame.deliver(
            delivery_id="d1",
            session_id="orch",
            origin="wechat:direct:acct:user_sender",
            text="hello",
        )
    )

    await client.send_outbound("event text 2")
    assert ipc.sent == [
        ("wechat:direct:*:*", "event text"),
        ("wechat:direct:*:*", "event text 2"),
    ]


@pytest.mark.asyncio
async def test_orchestrator_pending_outbound_stays_queued_when_flush_is_rejected() -> None:
    """A gateway NACK during flush must not silently drop queued events."""
    from orchestratord.im_gateway_client import OrchestratorGatewayClient
    from orchestratord.ipc.protocol import GatewayFrame

    clock = [1000.0]

    class _FakeIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[tuple[str, str]] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append((origin, text))
            return GatewayFrame.nack(delivery_id="d1", reason="send failed")

    ipc = _FakeIpc()
    client = OrchestratorGatewayClient(
        _handlers(),
        ipc_client=ipc,
        origin="wechat:direct:*:*",
        clock=lambda: clock[0],
        pending_retry_base_seconds=60.0,
    )
    await client.send_outbound("event text")
    # The wildcard send is attempted immediately (the gateway resolves it);
    # it is NACKed here, so the event is queued rather than dropped.
    assert ipc.sent == [("wechat:direct:*:*", "event text")]
    assert list(client._pending_outbound) == ["event text"]

    await client._flush_pending_outbound()

    # The retry cooldown prevents heartbeat flushes from hammering WeChat
    # after a rate limit / send failure.
    assert ipc.sent == [("wechat:direct:*:*", "event text")]
    assert list(client._pending_outbound) == ["event text"]

    clock[0] += 60.0
    await client._flush_pending_outbound()

    # Once the cooldown expires, flush retries against the same wildcard
    # origin; still NACKed, so the event stays queued.
    assert ipc.sent == [
        ("wechat:direct:*:*", "event text"),
        ("wechat:direct:*:*", "event text"),
    ]
    assert list(client._pending_outbound) == ["event text"]


@pytest.mark.asyncio
async def test_orchestrator_outbound_timeout_does_not_queue_retry() -> None:
    """ACK timeout is ambiguous: the IM message may already be delivered.

    Do not enqueue an automatic retry, otherwise Feishu/WeChat can receive
    duplicate messages when gateway.send succeeds but the IPC ACK arrives
    after the client timeout.
    """
    from orchestratord.im_gateway_client import OrchestratorGatewayClient

    class _TimeoutIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[tuple[str, str]] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append((origin, text))

    ipc = _TimeoutIpc()
    client = OrchestratorGatewayClient(_handlers(), ipc_client=ipc, origin="im:direct:*:*")

    await client.send_outbound("event text")

    assert ipc.sent == [("im:direct:*:*", "event text")]
    assert list(client._pending_outbound) == []


@pytest.mark.asyncio
async def test_orchestrator_pending_outbound_timeout_is_dropped_not_retried() -> None:
    """A queued message that times out after send attempt must be removed.

    This avoids repeated duplicate deliveries when the gateway did send the
    IM message but returned the ACK too late.
    """
    from orchestratord.im_gateway_client import OrchestratorGatewayClient

    class _TimeoutIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[tuple[str, str]] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append((origin, text))

    ipc = _TimeoutIpc()
    client = OrchestratorGatewayClient(_handlers(), ipc_client=ipc, origin="im:direct:*:*")
    client._queue_pending_outbound("event text")

    await client._flush_pending_outbound()

    assert ipc.sent == [("im:direct:*:*", "event text")]
    assert list(client._pending_outbound) == []


@pytest.mark.asyncio
async def test_orchestrator_send_outbound_queues_when_not_connected() -> None:
    """Events emitted before the IPC connection is up must queue, not raise.

    The orchestrator daemon emits ``orchestrator.started`` from
    ``_orch_run_patched`` which races the heartbeat loop's
    ``_connect_and_register``. Before the socket is open, ``send_outbound``
    must not propagate ``RuntimeError("not connected")`` — it queues the
    event so the post-register flush can deliver it.
    """
    from orchestratord.im_gateway_client import OrchestratorGatewayClient

    class _NotConnectedIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[tuple[str, str]] = []
            self.connected = False

        async def send_outbound(self, *, origin, text):
            if not self.connected:
                raise RuntimeError("not connected")
            self.sent.append((origin, text))
            from orchestratord.ipc.protocol import GatewayFrame

            return GatewayFrame.ack(delivery_id="d", layer="processed", message="sent")

    ipc = _NotConnectedIpc()
    client = OrchestratorGatewayClient(_handlers(), ipc_client=ipc, origin="wechat:direct:*:*")

    # Not connected yet — must not raise; the event is queued.
    await client.send_outbound("startup event")
    assert ipc.sent == []
    assert list(client._pending_outbound) == ["startup event"]

    # Connection comes up (register succeeds). Flush delivers the queued
    # event to the wildcard origin — the gateway resolves it.
    ipc.connected = True
    await client._flush_pending_outbound()
    assert ipc.sent == [("wechat:direct:*:*", "startup event")]
    assert list(client._pending_outbound) == []


@pytest.mark.asyncio
async def test_orchestrator_pending_outbound_dedupes_identical_text() -> None:
    """Duplicate texts (e.g. repeated 'IM notifications enabled' on reconnect)
    must not queue multiple copies — otherwise the operator sees the same
    message N times when the pending queue finally flushes."""
    from orchestratord.im_gateway_client import OrchestratorGatewayClient

    class _NotConnectedIpc:
        def __init__(self):
            self.on_deliver = None

        async def send_outbound(self, *, origin, text):
            raise RuntimeError("not connected")

    ipc = _NotConnectedIpc()
    client = OrchestratorGatewayClient(_handlers(), ipc_client=ipc, origin="wechat:direct:*:*")

    # Same text emitted 3 times (e.g. 3 reconnects) — only 1 should queue.
    for _ in range(3):
        await client.send_outbound("orchestratord: IM notifications enabled")
    assert list(client._pending_outbound) == ["orchestratord: IM notifications enabled"]

    # A different text is queued separately.
    await client.send_outbound("issue.started event")
    assert list(client._pending_outbound) == [
        "orchestratord: IM notifications enabled",
        "issue.started event",
    ]


@pytest.mark.asyncio
async def test_orchestrator_pending_duplicate_does_not_send_while_queued() -> None:
    """A duplicate event already pending must not bypass the queue and send now."""
    from orchestratord.im_gateway_client import OrchestratorGatewayClient
    from orchestratord.ipc.protocol import GatewayFrame

    clock = [1000.0]

    class _NackingIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[str] = []

        async def send_outbound(self, *, origin, text):
            self.sent.append(text)
            return GatewayFrame.nack(delivery_id="d1", reason="send failed: rate limited")

    ipc = _NackingIpc()
    client = OrchestratorGatewayClient(
        _handlers(),
        ipc_client=ipc,
        origin="wechat:direct:*:*",
        clock=lambda: clock[0],
        pending_retry_base_seconds=60.0,
    )

    text = "orchestratord: IM notifications enabled"
    await client.send_outbound(text)
    await client.send_outbound(text)

    assert ipc.sent == [text]
    assert list(client._pending_outbound) == [text]


@pytest.mark.asyncio
async def test_orchestrator_inbound_flush_bypasses_pending_retry_cooldown() -> None:
    """A new inbound WeChat message can refresh context and should trigger a flush."""
    from orchestratord.im_gateway_client import OrchestratorGatewayClient
    from orchestratord.ipc.protocol import GatewayFrame

    clock = [1000.0]

    class _FlakyIpc:
        def __init__(self):
            self.on_deliver = None
            self.sent: list[str] = []
            self.fail = True

        async def send_outbound(self, *, origin, text):
            self.sent.append(text)
            if self.fail:
                return GatewayFrame.nack(delivery_id="d1", reason="send failed: rate limited")
            return GatewayFrame.ack(delivery_id="d2", layer="processed", message="sent")

    ipc = _FlakyIpc()
    client = OrchestratorGatewayClient(
        _handlers(),
        ipc_client=ipc,
        origin="wechat:direct:*:*",
        clock=lambda: clock[0],
        pending_retry_base_seconds=60.0,
    )

    await client.send_outbound("event text")
    assert ipc.sent == ["event text"]
    assert list(client._pending_outbound) == ["event text"]

    ipc.fail = False
    await client._flush_pending_outbound()
    assert ipc.sent == ["event text"]

    await client._on_pushed_deliver(
        GatewayFrame.deliver(
            delivery_id="d1",
            session_id="orch",
            origin="wechat:direct:acct:user_sender",
            text="hello",
        )
    )

    assert ipc.sent == ["event text", "event text"]
    assert list(client._pending_outbound) == []


@pytest.mark.asyncio
async def test_orchestrator_pending_outbound_concurrent_flush_no_index_error() -> None:
    """Two concurrent _flush_pending_outbound calls must not cause IndexError.

    The heartbeat loop and the inbound push handler can both call
    _flush_pending_outbound at the same time. Without a lock, both peek
    self._pending_outbound[0] before an await, then both try popleft(),
    causing IndexError: pop from an empty deque.
    """
    from orchestratord.im_gateway_client import OrchestratorGatewayClient
    from orchestratord.ipc.protocol import GatewayFrame

    class _SlowIpc:
        """IPC that yields control during send_outbound to simulate concurrency."""

        def __init__(self):
            self.on_deliver = None
            self.sent: list[str] = []

        async def send_outbound(self, *, origin, text):
            await asyncio.sleep(0)  # yield to event loop — lets the other flush start
            self.sent.append(text)
            return GatewayFrame.ack(delivery_id="d", layer="processed", message="sent")

    ipc = _SlowIpc()
    client = OrchestratorGatewayClient(_handlers(), ipc_client=ipc, origin="wechat:direct:*:*")
    # Queue one item directly (bypass send_outbound which would send immediately).
    client._queue_pending_outbound("event text")
    assert list(client._pending_outbound) == ["event text"]

    # Launch two concurrent flushes — must not raise IndexError.
    await asyncio.gather(
        client._flush_pending_outbound(),
        client._flush_pending_outbound(),
    )

    # The item should have been sent exactly once (the lock serialises).
    assert ipc.sent == ["event text"]
    assert list(client._pending_outbound) == []


@pytest.mark.asyncio
async def test_build_ipc_deliver_sends_outbound_via_im_client() -> None:
    """build_ipc_deliver ships formatted events over the IM client."""
    from orchestratord.sinks.channel import build_ipc_deliver

    sent: list[str] = []

    class _FakeIm:
        async def send_outbound(self, text):
            sent.append(text)

    deliver = build_ipc_deliver(_FakeIm())
    deliver(OrchestratorEvent("issue.failed", "I1", EventLevel.ERROR, "boom"), "boom text")
    await asyncio.sleep(0.05)  # let the scheduled task run
    assert sent == ["boom text"]


def test_orchestrator_build_session_sink_emits_issue_started(tmp_path) -> None:
    """A session sink creation should immediately notify IM that work started."""
    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.orchestrator import Orchestrator

    received: list[tuple[str, EventLevel, str]] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.workflow = WorkflowConfig()
    orchestrator.im_event_deliver = lambda event, text: received.append(
        (event.event_type, event.level, text)
    )
    orchestrator._im_emitters = {}

    orchestrator._build_session_sink("I1")

    assert received
    assert received[0][0] == "issue.started"
    assert received[0][1] is EventLevel.INFO


def test_orchestrator_emit_im_event_reaches_issue_emitter(tmp_path) -> None:
    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.orchestrator import Orchestrator

    received: list[OrchestratorEvent] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.workflow = WorkflowConfig()
    orchestrator.im_event_deliver = None
    orchestrator._im_emitters = {
        "I1": OrchestratorEventEmitter("I1", sinks=[received.append]),
    }

    orchestrator._emit_im_event(
        "I1",
        "verification.failed",
        EventLevel.WARN,
        "pytest failed",
    )

    assert [(event.event_type, event.level, event.message) for event in received] == [
        ("verification.failed", EventLevel.WARN, "pytest failed")
    ]


def test_orchestrator_emit_im_event_can_use_daemon_deliver(tmp_path) -> None:
    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.orchestrator import Orchestrator

    received: list[str] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.workflow = WorkflowConfig()
    orchestrator._im_emitters = {}
    orchestrator.im_event_deliver = lambda event, text: received.append(event.event_type)

    orchestrator._emit_im_event(
        "",
        "orchestrator.started",
        EventLevel.INFO,
        "started",
    )

    assert received == ["orchestrator.started"]


def test_orchestrator_control_stop_emits_im_event() -> None:
    from orchestratord.orchestrator import Orchestrator

    class _PauseResume:
        def __init__(self):
            self.set_calls = 0

        def set(self):
            self.set_calls += 1

        def clear(self):
            return None

    received: list[OrchestratorEvent] = []
    issue_id = "I1"
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._state = SimpleNamespace(
        running={
            issue_id: SimpleNamespace(
                status="running",
                pause_resume_event=_PauseResume(),
            )
        },
        pending_review=set(),
        completed=set(),
        claimed=set(),
    )
    orchestrator._issue_tasks = {}
    orchestrator._registry = SimpleNamespace(_records={})
    orchestrator._im_emitters = {
        issue_id: OrchestratorEventEmitter(issue_id, sinks=[received.append]),
    }
    orchestrator.im_event_deliver = None

    orchestrator._apply_control_command("stop", issue_id, "")

    assert any(event.event_type == "control.stop" for event in received)


@pytest.mark.asyncio
async def test_review_reject_retries_pending_review_issue_with_feedback(tmp_path) -> None:
    from orchestratord.cli.issue import _run_review
    from orchestratord.issue_clarifier.queue import ClarificationQueue
    from orchestratord.issue_registry import IssueRegistry, IssueStatus
    from orchestratord.orchestrator import Orchestrator
    from orchestratord.tracker import Intent

    issue_id = "5"
    feedback = "注释没有中文\n请补充说明"
    registry_path = tmp_path / ".orchestratord_issue_registry.json"
    registry = IssueRegistry(registry_path)
    registry.register(issue_id, issue_id)
    registry.mark_pending_review(issue_id)

    args = argparse.Namespace(
        id=issue_id,
        approve=False,
        reject=True,
        feedback=feedback,
        comment=None,
    )
    assert _run_review(registry_path, args, workspace_root=tmp_path) == 0
    assert IssueRegistry(registry_path).get(issue_id).status is IssueStatus.PENDING_REVIEW

    tracker_updates: list[tuple[str, str]] = []

    class _Tracker:
        async def update_issue_state(self, actual_issue_id: str, state: str) -> None:
            tracker_updates.append((actual_issue_id, state))

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._workspace_root = tmp_path
    orchestrator._state = SimpleNamespace(
        running={},
        pending_review={issue_id},
        completed={issue_id},
        claimed={issue_id},
    )
    orchestrator._registry = registry
    orchestrator._clarification_queue = ClarificationQueue(
        tmp_path / ".orchestratord_clarification_queue.json"
    )
    orchestrator.tracker = _Tracker()
    orchestrator._im_emitters = {}
    orchestrator.im_event_deliver = None
    # 局部构造绕过 __init__，补绑 C2b 应用侧协作对象。
    from orchestratord.applications.issue_pr.lifecycle import IssueToPrLifecycle

    orchestrator._issue_app = IssueToPrLifecycle(orchestrator)

    await orchestrator._process_control_commands()

    record = registry.get(issue_id)
    assert record is not None
    assert record.status is IssueStatus.PENDING
    assert record.attempt_count == 1
    assert record.intent is Intent.FOLLOWUP
    assert record.intent_source == "cli"
    assert issue_id not in orchestrator._state.pending_review
    assert issue_id not in orchestrator._state.completed
    assert issue_id not in orchestrator._state.claimed
    assert tracker_updates == [(issue_id, "open")]
    queued_feedback = orchestrator._clarification_queue.get(issue_id)
    assert queued_feedback is not None
    assert queued_feedback.question == f"[Human Review Rejected] {feedback}"

    # A repeated rejection while the retry is queued is idempotent and can
    # update the operator feedback instead of failing on status=pending.
    updated_feedback = "注释仍然没有中文"
    args.feedback = updated_feedback
    assert _run_review(registry_path, args, workspace_root=tmp_path) == 0
    await orchestrator._process_control_commands()

    record = registry.get(issue_id)
    assert record is not None
    assert record.status is IssueStatus.PENDING
    assert record.attempt_count == 1
    assert record.intent is Intent.FOLLOWUP
    assert tracker_updates == [(issue_id, "open"), (issue_id, "open")]
    queued_feedback = orchestrator._clarification_queue.get(issue_id)
    assert queued_feedback is not None
    assert queued_feedback.question == f"[Human Review Rejected] {updated_feedback}"


@pytest.mark.asyncio
async def test_review_approve_syncs_daemon_state_and_remote_tracker(tmp_path) -> None:
    from orchestratord.cli.issue import _run_review
    from orchestratord.issue_registry import IssueRegistry, IssueStatus
    from orchestratord.orchestrator import Orchestrator

    issue_id = "7"
    registry_path = tmp_path / ".orchestratord_issue_registry.json"
    registry = IssueRegistry(registry_path)
    registry.register(issue_id, issue_id, branch_name="orchestratord/issue-7")
    registry.mark_synced(
        issue_id,
        branch_name="orchestratord/issue-7",
        commit_sha="abc123",
        pr_number="7",
        pr_url="https://gitcode.example/pulls/7",
    )
    registry.mark_pending_review(issue_id)

    args = argparse.Namespace(
        id=issue_id,
        approve=True,
        reject=False,
        feedback=None,
        comment="LGTM",
    )
    assert _run_review(registry_path, args, workspace_root=tmp_path) == 0

    tracker_updates: list[tuple[str, str]] = []
    tracker_comments: list[tuple[str, str]] = []

    class _Tracker:
        async def update_issue_state(self, actual_issue_id: str, state: str) -> None:
            tracker_updates.append((actual_issue_id, state))

        async def create_comment(self, actual_issue_id: str, body: str) -> None:
            tracker_comments.append((actual_issue_id, body))

    delivered: list[OrchestratorEvent] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._workspace_root = tmp_path
    orchestrator._state = SimpleNamespace(
        pending_review={issue_id},
        claimed={issue_id},
        completed=set(),
    )
    orchestrator._registry = registry
    orchestrator.tracker = _Tracker()
    orchestrator._im_emitters = {}
    orchestrator.im_event_deliver = lambda event, _text: delivered.append(event)
    # 局部构造绕过 __init__，补绑 C2b 应用侧协作对象。
    from orchestratord.applications.issue_pr.lifecycle import IssueToPrLifecycle

    orchestrator._issue_app = IssueToPrLifecycle(orchestrator)

    await orchestrator._process_control_commands()

    record = registry.get(issue_id)
    assert record is not None
    assert record.status is IssueStatus.COMPLETED
    assert issue_id not in orchestrator._state.pending_review
    assert issue_id not in orchestrator._state.claimed
    assert issue_id in orchestrator._state.completed
    assert tracker_updates == [(issue_id, "completed")]
    assert tracker_comments == [(issue_id, "## Approved\n\nLGTM")]
    assert any(event.event_type == "issue.completed" for event in delivered)

    # Reissuing approve repairs a remote label that missed the first sync.
    assert _run_review(registry_path, args, workspace_root=tmp_path) == 0
    await orchestrator._process_control_commands()
    assert tracker_updates == [(issue_id, "completed"), (issue_id, "completed")]
    assert tracker_comments == [(issue_id, "## Approved\n\nLGTM")]


def test_orchestrator_emit_issue_detected_includes_url(tmp_path) -> None:
    """_emit_im_event for issue.detected carries the issue URL in payload."""
    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.orchestrator import Orchestrator

    received: list[OrchestratorEvent] = []
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.workflow = WorkflowConfig()
    orchestrator.im_event_deliver = None
    orchestrator._im_emitters = {
        "AGENTSDK-15": OrchestratorEventEmitter("AGENTSDK-15", sinks=[received.append]),
    }

    orchestrator._emit_im_event(
        "AGENTSDK-15",
        "issue.detected",
        EventLevel.INFO,
        "新增 ISSUE",
        {
            "title": "Fix login bug",
            "repo": "owner/repo",
            "url": "https://gitcode.com/owner/repo/issues/15",
        },
    )

    assert len(received) == 1
    event = received[0]
    assert event.event_type == "issue.detected"
    assert event.level is EventLevel.INFO
    assert event.payload["url"] == "https://gitcode.com/owner/repo/issues/15"
    assert event.payload["title"] == "Fix login bug"

    # Verify the formatted text includes the URL
    from orchestratord.events.formatter import format_event

    txt = format_event(event)
    assert "新增 ISSUE" in txt
    assert "ISSUE-Fix login bug" in txt
    assert "https://gitcode.com/owner/repo/issues/15" in txt
