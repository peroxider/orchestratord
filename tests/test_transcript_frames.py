"""Transcript rows must carry role + claude-style content.

Historical defects:

* transcript rows were raw broadcast frames (no ``role`` field) →
  ``issue transcript --role assistant`` matched 0 of 123 rows;
* the transcript was only written when a control-socket client was
  attached, so ``issue tail`` / ``run logs`` found no file for a run
  without a socket — and nothing was written incrementally anyway.

The notification pump makes events flow through ``_process_events`` during the
turn; combined with the frame→message conversion here, the transcript
is written incrementally with rows the readers can filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.runner_utils import _broadcast_to_socket
from orchestratord.spi.events import EventEnvelope, EventKind


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _session() -> SimpleNamespace:
    return SimpleNamespace(run_id="run-t1", control_socket=None)


async def _broadcast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: EventEnvelope
) -> Path:
    home = _home(tmp_path, monkeypatch)
    await _broadcast_to_socket(_session(), event)
    return home / ".orchestratord" / "sessions" / "run-t1" / "transcript.jsonl"


async def test_text_event_writes_assistant_role(tmp_path, monkeypatch) -> None:
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(
            seq=1, timestamp=0, kind=EventKind.TEXT, payload={"text": "hello"}
        ),
    )
    assert path.exists(), "transcript must be written without a control socket"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["role"] == "assistant"
    assert rows[0]["type"] == "TextDelta"
    assert rows[0]["content"][0] == {"type": "text", "text": "hello"}


async def test_tool_call_event_writes_assistant_tool_use(tmp_path, monkeypatch) -> None:
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(
            seq=1,
            timestamp=0,
            kind=EventKind.TOOL_CALL,
            payload={"call_id": "c1", "name": "bash", "arguments": {"cmd": "ls"}},
        ),
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["role"] == "assistant"
    block = rows[0]["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "c1"
    assert block["name"] == "bash"


async def test_tool_result_event_writes_user_tool_result(tmp_path, monkeypatch) -> None:
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(
            seq=1,
            timestamp=0,
            kind=EventKind.TOOL_RESULT,
            payload={"call_id": "c1", "ok": True, "output": "file-a\nfile-b"},
        ),
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["role"] == "user"
    block = rows[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "c1"
    assert block["content"] == "file-a\nfile-b"


async def test_role_filter_finds_assistant_rows(tmp_path, monkeypatch) -> None:
    """The end-to-end read contract: role filter must match assistant rows."""
    path = await _broadcast(
        tmp_path,
        monkeypatch,
        EventEnvelope(seq=1, timestamp=0, kind=EventKind.TEXT, payload={"text": "hi"}),
    )
    await _broadcast_to_socket(
        _session(),
        EventEnvelope(
            seq=2,
            timestamp=0,
            kind=EventKind.TOOL_RESULT,
            payload={"call_id": "c1", "ok": True, "output": "out"},
        ),
    )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assistant = [r for r in rows if r.get("role") == "assistant"]
    user = [r for r in rows if r.get("role") == "user"]
    assert len(assistant) == 1
    assert len(user) == 1


async def test_broken_live_socket_does_not_drop_durable_history(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)

    class BrokenSocket:
        async def send_event(self, frame):
            raise OSError("client disconnected")

    session = _session()
    session.control_socket = BrokenSocket()
    await _broadcast_to_socket(
        session,
        EventEnvelope(
            seq=1,
            timestamp=1,
            kind=EventKind.TEXT,
            payload={"text": "durable reply"},
        ),
    )
    assert (home / ".orchestratord/sessions/run-t1/transcript.jsonl").exists()


async def test_backend_send_records_exact_input_before_execution(tmp_path, monkeypatch):
    from orchestratord import event_tailer
    from orchestratord.backend_runner import BackendRunner
    from orchestratord.event_tailer import read_history_direct

    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(event_tailer, "SESSIONS_DIR", home / ".orchestratord/sessions")
    sent = []

    class BackendSession:
        async def send(self, text):
            history = read_history_direct("run-t1")
            assert history[0]["content"] == [{"type": "text", "text": text}]
            sent.append(text)

        async def close(self):
            pass

    async def no_socket(session, **kwargs):
        return False

    async def no_events(*args, **kwargs):
        pass

    runner = object.__new__(BackendRunner)
    caps = SimpleNamespace(
        **dict.fromkeys(
            [
                "streaming_deltas",
                "resumable",
                "interrupt",
                "approval_hooks",
                "parallel_sessions",
                "cost_reporting",
                "tool_filtering",
                "takeover",
            ],
            False,
        )
    )
    runner.backend = SimpleNamespace(
        name="fixture",
        capabilities=lambda: caps,
        create_session=lambda spec: BackendSession(),
    )
    runner.agent_config = SimpleNamespace(permission_mode="default")
    runner._start_control_socket = no_socket
    runner._preflight_spec = lambda *args: True
    runner._process_events = no_events
    runner._resolve_timeouts = lambda spec: dict.fromkeys(
        [
            "total",
            "handshake",
            "first_turn",
            "inactivity",
            "idle_watchdog",
        ],
        10,
    )
    session = _session()
    session.issue = SimpleNamespace(id="task")  # legacy read face consumed by backend_runner
    session.workspace = SimpleNamespace(path=tmp_path)
    session._user_prompt = "Exact task\nincluding requirements <script>"
    spec = SimpleNamespace(resume_session_id=None, system_prompt="Workflow rules")
    await runner._run_with_backend(session, spec, None, None, None, None)
    message = read_history_direct("run-t1")[0]
    assert sent == [session._user_prompt]
    assert message["origin"] == "orchestrator"
    assert message["system_prompt"] == "Workflow rules"
    assert message["delivery"] == "submitted"


@pytest.mark.parametrize("failure", ["invalid_spec", "cancelled"])
async def test_runner_records_terminal_even_without_backend_completion(
    tmp_path, monkeypatch, failure
):
    import asyncio
    from unittest.mock import AsyncMock

    from orchestratord.backend_runner import BackendRunner
    from orchestratord.config.schema import AgentConfig, SandboxConfig
    from orchestratord.session_state import RunSession, RunSubject
    from orchestratord.workspace import Workspace

    home = _home(tmp_path, monkeypatch)
    runner = BackendRunner(
        SimpleNamespace(name="fixture"), AgentConfig(), SandboxConfig()
    )
    monkeypatch.setattr(runner, "_append_skill_index", lambda text: text)
    session = RunSession(
        subject=RunSubject(id="task", identifier="task"),
        workspace=Workspace(path=tmp_path, issue_identifier="task"),
        prompt_override="task input",
    )
    if failure == "invalid_spec":
        runner.agent_config.run_timeout_ms = 1
        await runner.run(session, None)
    else:
        monkeypatch.setattr(
            runner, "_run_with_backend", AsyncMock(side_effect=asyncio.CancelledError)
        )
        with pytest.raises(asyncio.CancelledError):
            await runner.run(session, None)
    rows = [
        json.loads(line)
        for line in (
            home / ".orchestratord/sessions" / session.run_id / "transcript.jsonl"
        )
        .read_text()
        .splitlines()
    ]
    assert rows[-1]["type"] == "RunEnded"
    assert rows[-1]["data"]["status"] == "failed"
    assert rows[-1]["data"]["reason"] == (
        "cancelled" if failure == "cancelled" else "backend_error"
    )
