"""Copilot backend tests — wire-format translation of the JSONL stream.

The integration tests spawn the scripted fake Copilot CLI
(``_fake_copilot_cli.py``) via ``sys.executable`` (wired through
``SessionSpec.runtime_bin``) and drive ``CopilotSession`` through its real
invocation shape — ``copilot -p "<prompt>" --output-format json …`` with
the prompt on the command line and NDJSON events on stdout. They cover:
delta streaming (``assistant.message_delta`` → ``TEXT_DELTA``), the
authoritative-message duplicate guard, tool events, error propagation
(``session.error``), turn/session completion, and the total timeout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from orchestratord_copilot.backend import CopilotBackend
from orchestratord_copilot.session import CopilotSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind
from orchestratord.spi.session import ResumeStatus

_FAKE_CLI = Path(__file__).with_name("_fake_copilot_cli.py")

_PROMPT = "list the repo files"


def _spec(mode: str = "happy", **kw: object) -> SessionSpec:
    """SessionSpec wired to the fake CLI through runtime_bin.

    A ``runtime_bin`` pointing at a ``.py`` script is launched via
    ``sys.executable`` by the session, so the fake is a drop-in for the
    real ``copilot`` binary. The fake's behavior is selected through the
    environment (which the session forwards to the child).
    """
    return SessionSpec(
        cwd="/tmp",
        runtime_bin=str(_FAKE_CLI),
        env={"FAKE_COPILOT_MODE": mode},
        **kw,  # type: ignore[arg-type]
    )


async def _send_and_collect(
    session: CopilotSession, prompt: str = _PROMPT
) -> list:
    await session.send(prompt)
    return [ev async for ev in session.events()]


# -- capabilities --------------------------------------------------------


def test_session_capabilities_match_backend() -> None:
    backend = CopilotBackend()
    session = CopilotSession(_spec())
    assert session.capabilities == backend.capabilities()
    # assistant.message_delta carries genuine incremental fragments.
    assert session.capabilities.streaming_deltas is True


# -- happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_streams_deltas_and_tool_events() -> None:
    session = CopilotSession(_spec("happy"))
    try:
        events = await _send_and_collect(session)
    finally:
        await session.close()

    assert [ev.kind for ev in events] == [
        EventKind.TEXT_DELTA,
        EventKind.TEXT_DELTA,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    # Deltas stream the prompt echoed by the fake (proving the -p argv
    # channel) followed by the rest of the answer.
    assert events[0].payload["text"] == f"PROMPT:{_PROMPT} "
    assert events[1].payload["text"] == "final answer"
    # The authoritative assistant.message must NOT be re-emitted as TEXT —
    # the chat bridge folds TEXT/TEXT_DELTA, so a re-emit would duplicate.
    assert not any(ev.kind is EventKind.TEXT for ev in events)
    # tool-only turn: toolRequests → TOOL_CALL, execution → TOOL_RESULT.
    assert events[2].payload == {
        "call_id": "tc-1",
        "name": "shell",
        "arguments": {"command": "ls"},
        "session_id": "copilot-fake-123",
    }
    assert events[3].payload == {
        "call_id": "tc-1",
        "ok": True,
        "output": "file-a\nfile-b",
    }
    assert events[4].payload == {"reason": "success"}
    # The native session id from session.start reaches SESSION_COMPLETE.
    assert events[5].payload == {
        "reason": "success",
        "session_id": "copilot-fake-123",
    }


@pytest.mark.asyncio
async def test_nonstream_message_emits_text() -> None:
    """assistant.message without prior deltas → TEXT (defense path)."""
    session = CopilotSession(_spec("nonstream"))
    try:
        events = await _send_and_collect(session)
    finally:
        await session.close()

    kinds = [ev.kind for ev in events]
    assert kinds == [
        EventKind.TEXT,
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    assert events[0].payload["text"] == "plain answer"
    assert events[2].payload["session_id"] == "copilot-fake-ns"


# -- error propagation -----------------------------------------------------


@pytest.mark.asyncio
async def test_session_error_propagates_as_error() -> None:
    session = CopilotSession(_spec("error"))
    try:
        events = await _send_and_collect(session)
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "copilot_session_error"
    assert errors[0].payload["message"] == "authentication required"
    # The child also exits 1 — no duplicate exit ERROR on top of the
    # protocol error (mirrors Go's withCopilotExitCode composition).
    assert [ev.kind for ev in events][-2:] == [
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    assert events[-2].payload == {"reason": "error"}
    assert events[-1].payload["reason"] == "error"
    assert events[-1].payload["session_id"] == "copilot-fake-err"


@pytest.mark.asyncio
async def test_total_timeout_emits_timeout_terminal() -> None:
    session = CopilotSession(_spec("hang", total_timeout_s=0.5))
    try:
        events = await asyncio.wait_for(
            _send_and_collect(session), timeout=10.0
        )
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "copilot_timeout"
    assert [ev.kind for ev in events] == [
        EventKind.ERROR,
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    assert events[1].payload == {"reason": "timeout"}
    assert events[2].payload == {"reason": "timeout"}


# -- session protocol ------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_resume_three_state() -> None:
    session = CopilotSession(_spec())
    try:
        assert await session.probe_resume() is ResumeStatus.RESUMED
    finally:
        await session.close()

    resumed = CopilotSession(_spec(resume_session_id="copilot-fake-123"))
    try:
        # copilot has no cross-process resume probe.
        assert await resumed.probe_resume() is ResumeStatus.UNDETECTABLE
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_send_after_close_raises() -> None:
    session = CopilotSession(_spec())
    await session.close()
    with pytest.raises(RuntimeError, match="closed"):
        await session.send(_PROMPT)
