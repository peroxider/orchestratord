"""Cursor backend tests — wire-format translation of the stream-json stream.

The integration tests spawn the scripted fake Cursor CLI
(``_fake_cursor_cli.py``) via ``sys.executable`` (wired through
``SessionSpec.runtime_bin``) and drive ``CursorSession`` through its real
invocation shape — ``cursor-agent -p --output-format stream-json …`` with
the prompt on stdin and NDJSON events on stdout. They cover: text
extraction from ``assistant`` blocks (including the ``stdout:`` prefix
the real CLI sometimes emits), thinking deltas, nested ``tool_call``
pairs and legacy flat tool events, the result-event protocol boundary,
error propagation, turn/session completion, and the total timeout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from orchestratord_cursor.backend import CursorBackend
from orchestratord_cursor.session import CursorSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind
from orchestratord.spi.session import ResumeStatus

_FAKE_CLI = Path(__file__).with_name("_fake_cursor_cli.py")

_PROMPT = "summarize the tree"


def _spec(mode: str = "happy", **kw: object) -> SessionSpec:
    """SessionSpec wired to the fake CLI through runtime_bin.

    A ``runtime_bin`` pointing at a ``.py`` script is launched via
    ``sys.executable`` by the session, so the fake is a drop-in for the
    real ``cursor-agent`` binary. The fake's behavior is selected through
    the environment (which the session forwards to the child).
    """
    return SessionSpec(
        cwd="/tmp",
        runtime_bin=str(_FAKE_CLI),
        env={"FAKE_CURSOR_MODE": mode},
        **kw,  # type: ignore[arg-type]
    )


async def _send_and_collect(
    session: CursorSession, prompt: str = _PROMPT
) -> list:
    await session.send(prompt)
    return [ev async for ev in session.events()]


# -- capabilities --------------------------------------------------------


def test_session_capabilities_match_backend() -> None:
    backend = CursorBackend()
    session = CursorSession(_spec())
    assert session.capabilities == backend.capabilities()
    # assistant blocks carry complete text; the protocol has no
    # incremental text-fragment event, so no streaming-deltas claim.
    assert session.capabilities.streaming_deltas is False


# -- happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_translates_stream_in_order() -> None:
    session = CursorSession(_spec("happy"))
    try:
        events = await _send_and_collect(session)
    finally:
        await session.close()

    assert [ev.kind for ev in events] == [
        EventKind.TEXT,        # assistant text block ("stdout: " prefix stripped)
        EventKind.UNKNOWN,     # thinking delta
        EventKind.TOOL_CALL,   # tool_call started (nested readToolCall)
        EventKind.TOOL_RESULT,  # tool_call completed
        EventKind.TOOL_CALL,   # legacy tool_use
        EventKind.TOOL_RESULT,  # legacy tool_result
        EventKind.TEXT,        # final assistant output_text block
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    # The prompt echoed into the assistant block proves stdin delivery.
    assert events[0].payload["text"] == f"PROMPT:{_PROMPT} "
    assert events[1].payload["thinking"] == " pondering"
    # Nested envelope: name decoded from "readToolCall" minus the suffix;
    # the packed "call-1\nfc_9" id cut at the embedded newline.
    assert events[2].payload == {
        "call_id": "call-1",
        "name": "read",
        "arguments": {"path": "a.txt"},
        "session_id": "fake-cursor-1",
    }
    assert events[3].payload["call_id"] == "call-1"
    assert events[3].payload["ok"] is True
    assert "file body" in events[3].payload["output"]
    # Legacy flat tool events.
    assert events[4].payload == {
        "call_id": "toolu-2",
        "name": "edit_file",
        "arguments": {"path": "b.txt"},
        "session_id": "fake-cursor-1",
    }
    assert events[5].payload["output"] == "ok"
    assert events[6].payload["text"] == "All done"
    # The result event repeats the transcript — must NOT be re-emitted
    # (the Go port only fills in result text when nothing streamed).
    assert not any(
        ev.kind is EventKind.TEXT
        and ev.payload.get("native_type") == "result"
        for ev in events
    )
    assert events[7].payload == {"reason": "success"}
    assert events[8].payload == {
        "reason": "success",
        "session_id": "fake-cursor-1",
    }


# -- error propagation -----------------------------------------------------


@pytest.mark.asyncio
async def test_system_error_propagates_as_error() -> None:
    """system/subtype=error + exit 1 without a result → single ERROR."""
    session = CursorSession(_spec("error"))
    try:
        events = await _send_and_collect(session)
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "cursor_error"
    assert errors[0].payload["message"] == "provider unavailable"
    assert [ev.kind for ev in events][-2:] == [
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    assert events[-2].payload == {"reason": "error"}
    assert events[-1].payload["reason"] == "error"
    assert events[-1].payload["session_id"] == "fake-cursor-err"


@pytest.mark.asyncio
async def test_total_timeout_emits_timeout_terminal() -> None:
    session = CursorSession(_spec("hang", total_timeout_s=0.5))
    try:
        events = await asyncio.wait_for(
            _send_and_collect(session), timeout=10.0
        )
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "cursor_timeout"
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
    session = CursorSession(_spec())
    try:
        assert await session.probe_resume() is ResumeStatus.RESUMED
    finally:
        await session.close()

    resumed = CursorSession(_spec(resume_session_id="fake-cursor-1"))
    try:
        # cursor-agent has no cross-process resume probe.
        assert await resumed.probe_resume() is ResumeStatus.UNDETECTABLE
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_send_after_close_raises() -> None:
    session = CursorSession(_spec())
    await session.close()
    with pytest.raises(RuntimeError, match="closed"):
        await session.send(_PROMPT)
