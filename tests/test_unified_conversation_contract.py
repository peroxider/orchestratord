"""契约测试：统一 conversation_id 与跨 Agent 会话聚合。

本文件描述 FEATURE_UNIFIED_CONVERSATION_ID.md 的最小可验证契约。
这些契约在功能实现后作为正常回归测试运行；现有旧格式兼容性测试仍保留
在原有测试文件中。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.agent.task import AgentTask, AgentTaskResult
from orchestratord.issue_registry.models import IssueRecord
from orchestratord.run_store import RunRecord
from orchestratord.runner_utils import _broadcast_to_socket
from orchestratord.session_state import RunSession
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.workflow_engine.workflow_state import WorkflowState


def test_run_session_accepts_conversation_identity() -> None:
    session = RunSession(
        subject=SimpleNamespace(id="issue-1"),
        workspace=SimpleNamespace(path=Path(".")),
        conversation_id="conv-1",
        parent_run_id="run-previous",
    )

    assert session.conversation_id == "conv-1"
    assert session.parent_run_id == "run-previous"


def test_agent_task_carries_conversation_identity() -> None:
    task = AgentTask(
        id="task-1",
        conversation_id="conv-1",
    )

    assert task.conversation_id == "conv-1"
    assert task.to_template_dict()["conversation_id"] == "conv-1"


def test_agent_task_result_carries_conversation_identity() -> None:
    result = AgentTaskResult(
        task_id="task-1",
        conversation_id="conv-1",
        run_id="run-1",
    )

    assert result.conversation_id == "conv-1"


def test_issue_record_has_stable_conversation_identity() -> None:
    record = IssueRecord(
        issue_id="issue-1",
        issue_identifier="ISSUE-1",
        conversation_id="conv-1",
    )

    assert record.conversation_id == "conv-1"


def test_run_record_has_stable_conversation_identity() -> None:
    record = RunRecord(
        run_id="run-1",
        conversation_id="conv-1",
        workflow="demo",
        task_id="task-1",
        task_kind="generic",
    )

    assert record.conversation_id == "conv-1"


def test_workflow_state_has_conversation_identity() -> None:
    state = WorkflowState(
        workflow_name="demo",
        conversation_id="conv-1",
    )

    assert state.conversation_id == "conv-1"


def test_run_session_retains_backend_native_session_mapping() -> None:
    session = RunSession(
        subject=SimpleNamespace(id="issue-1"),
        workspace=SimpleNamespace(path=Path(".")),
        conversation_id="conv-1",
        run_id="run-1",
        backend_name="claude",
        backend_session_id="claude-native-1",
    )

    assert session.backend_name == "claude"
    assert session.backend_session_id == "claude-native-1"


def test_session_spec_carries_conversation_metadata_separately() -> None:
    spec = SessionSpec(
        cwd=".",
        resume_session_id="native-session-1",
        extra={
            "conversation_id": "conv-1",
            "parent_run_id": "run-0",
            "stage_id": "review",
            "branch_id": "branch-a",
        },
    )

    assert spec.resume_session_id == "native-session-1"
    assert spec.extra["conversation_id"] == "conv-1"
    assert spec.extra["parent_run_id"] == "run-0"


def test_agent_session_protocol_exposes_conversation_identity() -> None:
    from orchestratord.spi.session import AgentSession

    assert "conversation_id" in getattr(AgentSession, "__annotations__", {})


def test_ensure_conversation_id_is_stable_and_generates_missing_ids() -> None:
    from orchestratord.conversation_store import ensure_conversation_id

    assert ensure_conversation_id("conv-existing") == "conv-existing"
    generated = ensure_conversation_id()
    assert len(generated) == 36
    assert generated.count("-") == 4


@pytest.mark.asyncio
async def test_transcript_preserves_conversation_and_envelope_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    session = SimpleNamespace(
        run_id="run-1",
        conversation_id="conv-1",
        backend_name="opencode",
        backend_session_id="oc-native-1",
        stage_id="review",
        branch_id="branch-a",
        parent_run_id="run-0",
        control_socket=None,
    )
    event = EventEnvelope(
        seq=7,
        timestamp=123.45,
        kind=EventKind.TOOL_CALL,
        payload={
            "call_id": "call-1",
            "name": "Read",
            "arguments": {"path": "README.md"},
            "raw": {"provider_field": "preserve-me"},
        },
    )

    await _broadcast_to_socket(session, event)

    path = home / ".orchestratord" / "sessions" / "run-1" / "transcript.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["conversation_id"] == "conv-1"
    assert row["run_id"] == "run-1"
    assert row["backend"] == "opencode"
    assert row["backend_session_id"] == "oc-native-1"
    assert row["seq"] == 7
    assert row["kind"] == "tool_call"
    assert row["raw"]["provider_field"] == "preserve-me"


def test_conversation_manifest_indexes_parent_and_child_runs(tmp_path: Path) -> None:
    from orchestratord.conversation_store import ConversationStore

    store = ConversationStore(tmp_path)
    store.register_run(
        conversation_id="conv-1",
        run_id="run-1",
        backend="claude",
        backend_session_id="claude-1",
        stage_id="implementation",
    )
    store.register_run(
        conversation_id="conv-1",
        run_id="run-2",
        backend="opencode",
        backend_session_id="oc-2",
        stage_id="review",
        parent_run_id="run-1",
    )

    conversation = store.get("conv-1")
    assert [run["run_id"] for run in conversation["runs"]] == ["run-1", "run-2"]
    assert conversation["runs"][1]["parent_run_id"] == "run-1"


def test_dashboard_exposes_conversation_aggregation_api() -> None:
    from orchestratord.cli.dashboard import DashboardHandler

    assert hasattr(DashboardHandler, "_stream_conversation_events")


def test_opencode_approval_request_is_persistable() -> None:
    from orchestratord_opencode.session import OpenCodeSession
    from orchestratord.spi.backend import SessionSpec

    session = OpenCodeSession(SessionSpec(cwd="."))
    # Real opencode 1.18.x protocol: approvals arrive as
    # ``permission.v2.asked`` frames on the global bus (the pre-rewrite
    # ``_ingest_sse``/``approval.request`` seam no longer exists).
    session._ingest_frame(  # noqa: SLF001 - adapter contract seam
        {
            "id": "evt-1",
            "type": "permission.v2.asked",
            "data": {
                "sessionID": "ses_contract1",
                "id": "req-1",
                "action": "Shell",
                "resources": ["pwd"],
                "source": {
                    "type": "tool",
                    "messageID": "msg-1",
                    "callID": "call-1",
                },
            },
        },
        "ses_contract1",
    )

    events = []
    while not session._queue.empty():  # noqa: SLF001 - adapter contract seam
        events.append(session._queue.get_nowait())
    assert events[0].kind is EventKind.APPROVAL_REQUEST
    assert events[0].payload["request_id"] == "req-1"


def test_claude_thinking_and_system_events_are_not_silently_lost() -> None:
    from orchestratord_claude.session import ClaudeSession

    session = object.__new__(ClaudeSession)
    session._seq = 0  # noqa: SLF001 - adapter contract seam

    thinking = session._translate_event(  # noqa: SLF001 - adapter contract seam
        {
            "type": "assistant",
            "session_id": "claude-native-1",
            "message": {
                "content": [{"type": "thinking", "thinking": "internal plan"}],
            },
        }
    )
    system = session._translate_event(  # noqa: SLF001 - adapter contract seam
        {
            "type": "system",
            "session_id": "claude-native-1",
            "subtype": "init",
            "model": "claude-sonnet",
        }
    )

    assert thinking and thinking[0].payload["raw"]["thinking"] == "internal plan"
    assert system and system[0].payload["raw"]["subtype"] == "init"


def test_common_projection_does_not_drop_error_or_approval_fields() -> None:
    from orchestratord.runner_utils import _event_to_broadcast_dict

    for kind, payload in (
        (
            EventKind.ERROR,
            {"code": "provider_error", "message": "unavailable"},
        ),
        (
            EventKind.APPROVAL_REQUEST,
            {
                "request_id": "req-1",
                "call_id": "call-1",
                "tool_name": "Shell",
                "arguments": {"command": "pwd"},
            },
        ),
    ):
        projected = _event_to_broadcast_dict(
            EventEnvelope(seq=1, timestamp=1.0, kind=kind, payload=payload)
        )
        assert projected["code"] == payload.get("code", "")
        assert projected.get("request_id", "") == payload.get("request_id", "")


def test_maximum_common_projection_field_set() -> None:
    from orchestratord.runner_utils import _event_to_broadcast_dict

    event = EventEnvelope(
        seq=9,
        timestamp=10.5,
        kind=EventKind.TEXT_DELTA,
        payload={
            "text": "visible",
            "delta": "visible",
            "role": "assistant",
            "call_id": "call-1",
            "name": "Shell",
            "arguments": {"command": "pwd"},
            "output": "C:\\repo",
            "ok": True,
            "request_id": "req-1",
            "decision": "allow",
            "message": "Allow command?",
            "deny_reason": None,
            "thinking": "plan",
            "reasoning": "plan",
            "turn": 2,
            "turn_delta": 1,
            "phase": "review",
            "status": "running",
            "reason": "success",
            "code": "ok",
            "error_code": None,
            "error_message": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "total_cost_usd": 0.01,
            "raw": {"provider_field": "keep"},
        },
    )

    projected = _event_to_broadcast_dict(event)
    for key in (
        "text", "delta", "role", "call_id", "name", "arguments", "output",
        "ok", "request_id", "decision", "message", "thinking", "reasoning",
        "turn", "turn_delta", "phase", "status", "reason", "code",
        "usage", "total_cost_usd", "raw",
    ):
        assert key in projected


def test_current_common_event_projection_matrix() -> None:
    """The pre-conversation baseline must keep the common Web projection stable."""
    from orchestratord.runner_utils import _event_to_broadcast_dict

    cases = [
        (
            EventKind.TEXT,
            {"text": "hello"},
            {"content": "hello"},
        ),
        (
            EventKind.TEXT_DELTA,
            {"text": "chunk", "delta": "chunk"},
            {"content": "chunk"},
        ),
        (
            EventKind.TOOL_CALL,
            {"call_id": "c1", "name": "Read", "arguments": {"path": "a.py"}},
            {"tool_name": "Read", "tool_use_id": "c1", "params": {"path": "a.py"}},
        ),
        (
            EventKind.TOOL_RESULT,
            {"call_id": "c1", "ok": True, "output": "ok"},
            {"tool_use_id": "c1", "result": "ok"},
        ),
        (
            EventKind.TURN_COMPLETE,
            {"turn": 2},
            {"turn": 2},
        ),
        (
            EventKind.SESSION_COMPLETE,
            {"reason": "success"},
            {"reason": "success"},
        ),
    ]

    for kind, payload, expected in cases:
        event = EventEnvelope(seq=1, timestamp=1.0, kind=kind, payload=payload)
        projected = _event_to_broadcast_dict(event)
        for key, value in expected.items():
            assert projected[key] == value
