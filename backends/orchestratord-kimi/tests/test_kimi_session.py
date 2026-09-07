"""Kimi backend tests — descriptor drift guard and ACP wire translations.

The integration tests spawn the scripted fake ACP agent
(``_fake_kimi_cli.py``) through an executable ``sh`` shim that execs
``sys.executable``, so the real ``kimi`` binary is never touched.  They
drive ``KimiSession`` through the real ``kimi acp`` wire lifecycle
(ported from multica ``server/pkg/agent/kimi.go``): delta streaming,
deferred kimi tool-arg buffering, in-protocol permission auto-answers,
session/cancel, stopReason handling, stderr provider-error promotion,
and the resume / set_model request paths.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import fields
from pathlib import Path

import pytest
from orchestratord_kimi.backend import KimiBackend
from orchestratord_kimi.descriptor import KIMI_DESCRIPTOR
from orchestratord_kimi.session import KimiSession, kimi_tool_name_from_title

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

_FAKE_CLI = Path(__file__).with_name("_fake_kimi_cli.py")

# Keep the post-response notification quiescence short in tests (defaults
# mirror hermes.go: quiet=250ms, drain grace=2s).
_FAST_EXTRA = {"acp_quiet_s": "0.05", "acp_drain_grace_s": "0.2"}


def _fake_bin(tmp_path: Path, mode: str) -> str:
    """Executable shim: ``kimi`` replaced by the fake ACP agent.

    ``"$@"`` comes first so the fake sees the same argv shape as the real
    CLI (``kimi acp`` → ``argv[1] == "acp"``); the mode is the last arg.
    """
    shim = tmp_path / f"kimi-shim-{mode}"
    shim.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(_FAKE_CLI))} "
        f"\"$@\" {shlex.quote(mode)}\n"
    )
    shim.chmod(0o755)
    return str(shim)


def _spec(tmp_path: Path, mode: str, **kwargs: object) -> SessionSpec:
    return SessionSpec(
        cwd=str(tmp_path),
        runtime_bin=_fake_bin(tmp_path, mode),
        extra=dict(_FAST_EXTRA),
        **kwargs,  # type: ignore[arg-type]
    )


# -- descriptor layer ---------------------------------------------------


def test_descriptor_capabilities_match_backend() -> None:
    backend = KimiBackend()
    descriptor_bits = set(KIMI_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"kimi: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )
    assert "streaming_deltas" in backend_bits


def test_backend_name_and_session_defaults() -> None:
    backend = KimiBackend()
    assert backend.name == "kimi"
    session = KimiSession(SessionSpec(cwd="/tmp"))
    assert session.session_id.startswith("kimi-")
    assert session.capabilities.streaming_deltas is True
    assert session.capabilities.approval_hooks is False


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    with pytest.raises(RuntimeError, match="kimi"):
        KimiBackend().preflight(SessionSpec(cwd="/tmp"))


# -- wire-format helpers -------------------------------------------------


def test_tool_name_from_title() -> None:
    assert kimi_tool_name_from_title("Read file: /tmp/notes.md") == "read_file"
    assert kimi_tool_name_from_title("Run command: ls") == "terminal"
    assert kimi_tool_name_from_title("Web Search: cats") == "web_search"
    assert kimi_tool_name_from_title("Patch (replace)") == "edit_file"
    assert kimi_tool_name_from_title("Custom Tool") == "custom_tool"
    assert kimi_tool_name_from_title("") == ""


# -- resume probe ---------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_resume_contract() -> None:
    session = KimiSession(SessionSpec(cwd="/tmp"))
    try:
        assert await session.probe_resume() is ResumeStatus.RESUMED
    finally:
        await session.close()
    resumed = KimiSession(SessionSpec(cwd="/tmp", resume_session_id="s-1"))
    try:
        assert await resumed.probe_resume() is ResumeStatus.UNDETECTABLE
    finally:
        await resumed.close()


# -- integration round-trips ----------------------------------------------

_OnEvent = Callable[[EventEnvelope], Awaitable[None]]


async def _run_send_and_collect(
    session: KimiSession,
    on_event: _OnEvent | None = None,
    timeout: float = 10.0,
) -> list[EventEnvelope]:
    """Run ``send()`` in the background and collect events until it finishes."""
    send_task = asyncio.create_task(session.send("hello"))
    collected: list[EventEnvelope] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not send_task.done():
        async for ev in session.events():
            collected.append(ev)
            if on_event is not None:
                await on_event(ev)
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.005)
    await asyncio.wait_for(send_task, timeout=timeout)
    async for ev in session.events():
        collected.append(ev)
    return collected


@pytest.mark.asyncio
async def test_send_translates_event_stream_in_order(tmp_path: Path) -> None:
    session = KimiSession(_spec(tmp_path, "immediate"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    assert [ev.kind for ev in events] == [
        EventKind.TEXT_DELTA,
        EventKind.TEXT_DELTA,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    assert events[0].payload["text"] == "Hello "
    assert events[1].payload["text"] == "world"
    # Deferred kimi tool: TOOL_CALL lands only at completion, with the
    # cumulative streamed args parsed back into a dict.
    assert events[2].payload == {
        "call_id": "tc-1",
        "name": "terminal",
        "arguments": {"command": "ls -la"},
    }
    assert events[3].payload == {
        "call_id": "tc-1",
        "ok": True,
        "output": "total 0",
    }
    # rawInput tool: TOOL_CALL emitted on the start frame, exactly once.
    assert events[4].payload == {
        "call_id": "tc-2",
        "name": "read_file",
        "arguments": {"path": "/tmp/notes.md"},
    }
    assert events[5].payload == {
        "call_id": "tc-2",
        "ok": True,
        "output": "file contents",
    }
    assert events[6].payload == {"reason": "success"}
    assert events[7].payload == {"reason": "success"}
    assert [ev.seq for ev in events] == sorted(ev.seq for ev in events)


@pytest.mark.asyncio
async def test_permission_request_auto_answered(tmp_path: Path) -> None:
    """session/request_permission is answered in-protocol (no APPROVAL_REQUEST):
    the fake only emits `granted` after receiving the selected outcome."""
    session = KimiSession(_spec(tmp_path, "approval"))
    try:
        events = await _run_send_and_collect(session)
    finally:
        await session.close()

    kinds = [ev.kind for ev in events]
    assert EventKind.APPROVAL_REQUEST not in kinds
    texts = [
        ev.payload.get("text")
        for ev in events
        if ev.kind is EventKind.TEXT_DELTA
    ]
    assert "Hello " in texts
    assert "granted" in texts
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    assert turn.payload["reason"] == "success"


@pytest.mark.asyncio
async def test_interrupt_sends_session_cancel_and_aborts(
    tmp_path: Path,
) -> None:
    session = KimiSession(_spec(tmp_path, "cancel"))
    interrupted = False

    async def on_event(ev: EventEnvelope) -> None:
        nonlocal interrupted
        if ev.kind is EventKind.TEXT_DELTA and not interrupted:
            interrupted = True
            await session.interrupt()

    try:
        events = await _run_send_and_collect(session, on_event)
    finally:
        await session.close()

    assert interrupted
    texts = [
        ev.payload.get("text")
        for ev in events
        if ev.kind is EventKind.TEXT_DELTA
    ]
    assert "cancel-received" in texts
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    # kimi.go: stopReason=cancelled → status aborted.
    assert turn.payload["reason"] == "aborted"


@pytest.mark.asyncio
async def test_prompt_error_emits_error_and_fails_turn(tmp_path: Path) -> None:
    session = KimiSession(_spec(tmp_path, "prompt-error"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "kimi_prompt_error"
    assert errors[0].payload["message"] == "kimi exploded"
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    complete = next(ev for ev in events if ev.kind is EventKind.SESSION_COMPLETE)
    assert turn.payload["reason"] == "error"
    assert complete.payload["reason"] == "error"


@pytest.mark.asyncio
async def test_process_exit_before_result_fails_turn(tmp_path: Path) -> None:
    session = KimiSession(_spec(tmp_path, "exit"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert errors, "EOF before the prompt response must surface an ERROR"
    assert errors[0].payload["code"] == "kimi_acp_error"
    assert "kimi process exited" in errors[0].payload["message"]
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    assert turn.payload["reason"] == "error"


@pytest.mark.asyncio
async def test_stderr_provider_error_promotes_success_to_error(
    tmp_path: Path,
) -> None:
    """kimi.go promoteACPResultOnProviderError: end_turn + terminal stderr
    provider failure → the turn fails with the captured error."""
    session = KimiSession(_spec(tmp_path, "provider-error"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "kimi_provider_error"
    assert "provider.api_error: 401" in errors[0].payload["message"]
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    assert turn.payload["reason"] == "error"


@pytest.mark.asyncio
async def test_turn_timeout_emits_error_and_completes(tmp_path: Path) -> None:
    session = KimiSession(_spec(tmp_path, "hang", total_timeout_s=0.3))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert errors and errors[0].payload["code"] == "kimi_timeout"
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    assert turn.payload["reason"] == "timeout"
    complete = next(ev for ev in events if ev.kind is EventKind.SESSION_COMPLETE)
    assert complete.payload["reason"] == "timeout"


@pytest.mark.asyncio
async def test_resume_uses_session_resume(tmp_path: Path) -> None:
    session = KimiSession(_spec(tmp_path, "resume", resume_session_id="s-9"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    texts = [
        ev.payload.get("text")
        for ev in events
        if ev.kind is EventKind.TEXT_DELTA
    ]
    assert "resumed-ok" in texts
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    assert turn.payload["reason"] == "success"


@pytest.mark.asyncio
async def test_model_selection_via_set_model(tmp_path: Path) -> None:
    session = KimiSession(_spec(tmp_path, "model", model="k2"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    texts = [
        ev.payload.get("text")
        for ev in events
        if ev.kind is EventKind.TEXT_DELTA
    ]
    assert "model:k2" in texts
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    assert turn.payload["reason"] == "success"


@pytest.mark.asyncio
async def test_set_model_failure_fails_the_turn(tmp_path: Path) -> None:
    """kimi.go: set_model MUST fail the task — never silently fall back."""
    session = KimiSession(_spec(tmp_path, "setmodel-error", model="bad"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert errors and errors[0].payload["code"] == "kimi_acp_error"
    assert "could not switch to model 'bad'" in errors[0].payload["message"]
    turn = next(ev for ev in events if ev.kind is EventKind.TURN_COMPLETE)
    assert turn.payload["reason"] == "error"


@pytest.mark.asyncio
async def test_system_prompt_prepended_with_separator(tmp_path: Path) -> None:
    """kimi.go step 4: system prompt + '\\n\\n---\\n\\n' + prompt."""
    session = KimiSession(
        _spec(tmp_path, "echo", system_prompt="You are brief.")
    )
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    texts = [
        ev.payload.get("text")
        for ev in events
        if ev.kind is EventKind.TEXT_DELTA
    ]
    assert texts == ["You are brief.\n\n---\n\nhello"]


@pytest.mark.asyncio
async def test_send_after_close_raises(tmp_path: Path) -> None:
    session = KimiSession(_spec(tmp_path, "immediate"))
    await session.close()
    with pytest.raises(RuntimeError, match="session closed"):
        await session.send("hello")
