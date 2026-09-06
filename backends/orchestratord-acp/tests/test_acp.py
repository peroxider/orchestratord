"""ACP backend tests — descriptor, runtime, preflight, and stdio round-trips.

The integration tests spawn the scripted fake ACP server
(``_fake_acp_server.py``) as the "agent" subprocess and drive it through
``AcpSession``'s self-written JSON-RPC stdio client. They cover the three
hard requirements of the ACP adapter: streaming deltas
(``agent_message_chunk`` → ``TEXT_DELTA``), approval round-trips
(``session/request_permission`` → ``APPROVAL_REQUEST`` → ``approve`` →
``permission-granted``), and interrupt (``session/cancel`` →
``cancel-received``).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import fields
from pathlib import Path

import pytest
from orchestratord_acp.backend import AcpBackend
from orchestratord_acp.descriptor import (
    CODEBUDDY_DESCRIPTOR,
    DEVECO_DESCRIPTOR,
    GROK_DESCRIPTOR,
    QODERCLI_DESCRIPTOR,
    QODERCLICN_DESCRIPTOR,
    QWENPAW_DESCRIPTOR,
)
from orchestratord_acp.runtime import AcpRuntime, resolve_binary, resolve_runtime
from orchestratord_acp.session import AcpSession

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

_FAKE_SERVER = Path(__file__).with_name("_fake_acp_server.py")

_DESCRIPTORS = (
    (GROK_DESCRIPTOR, "grok"),
    (CODEBUDDY_DESCRIPTOR, "codebuddy"),
    (QWENPAW_DESCRIPTOR, "qwenpaw"),
    (QODERCLI_DESCRIPTOR, "qodercli"),
    (QODERCLICN_DESCRIPTOR, "qoderclicn"),
    (DEVECO_DESCRIPTOR, "deveco"),
)


def _test_runtime(mode: str | None = None) -> AcpRuntime:
    args = [str(_FAKE_SERVER)]
    if mode is not None:
        args.append(mode)
    return AcpRuntime(
        id="grok",
        display_name="Grok (ACP)",
        binary=sys.executable,
        cli_args=tuple(args),
        env_prefix="GROK_",
    )


# -- descriptor layer --------------------------------------------------


def test_descriptors_share_acp_package_and_family() -> None:
    for desc, prefer in _DESCRIPTORS:
        assert desc.backend_package == "orchestratord-acp"
        assert desc.family.value == "Protocol"
        assert desc.protocol_family == "acp"
        assert desc.extra_metadata["prefer"] == prefer


def test_one_protocol_family_carries_many_runtime_ids() -> None:
    """§8.4: one ``protocol_family`` ("acp") groups every ACP runtime id,
    each keeping its own ``name`` (= runtime_id)."""
    ids = {desc.name for desc, _ in _DESCRIPTORS}
    assert len(ids) == len(_DESCRIPTORS), "runtime ids must be unique"
    assert {desc.protocol_family for desc, _ in _DESCRIPTORS} == {"acp"}
    assert ids == {
        "grok",
        "codebuddy",
        "qwenpaw",
        "qodercli",
        "qoderclicn",
        "deveco",
    }


def test_descriptor_capabilities_match_backend() -> None:
    for desc, prefer in _DESCRIPTORS:
        backend = AcpBackend(prefer)
        descriptor_bits = set(desc.capabilities)
        backend_bits = {
            f.name
            for f in fields(backend.capabilities())
            if getattr(backend.capabilities(), f.name)
        }
        assert backend_bits == descriptor_bits, (
            f"{desc.name}: backend bits {backend_bits} drifted from "
            f"descriptor {descriptor_bits}"
        )


# -- runtime resolution -------------------------------------------------


def test_resolve_runtime_default_and_known_ids() -> None:
    assert resolve_runtime(None).id == "grok"
    assert resolve_runtime("codebuddy").id == "codebuddy"
    assert resolve_runtime("qwenpaw").id == "qwenpaw"


def test_resolve_runtime_rejects_unknown_id() -> None:
    with pytest.raises(ValueError):
        resolve_runtime("does-not-exist")


def test_resolve_binary_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = resolve_runtime("grok")
    monkeypatch.delenv("GROK_PATH", raising=False)
    assert resolve_binary(runtime, None) == "grok"
    monkeypatch.setenv("GROK_PATH", "/custom/grok")
    assert resolve_binary(runtime, None) == "/custom/grok"
    assert resolve_binary(runtime, "/explicit") == "/explicit"


# -- backend factory ----------------------------------------------------


def test_backend_name_and_display_name_follow_prefer() -> None:
    assert AcpBackend("grok").name == "grok"
    assert AcpBackend("codebuddy").name == "codebuddy"
    assert AcpBackend("qwenpaw").name == "qwenpaw"
    assert AcpBackend("grok").display_name == "Grok (ACP)"


def test_backend_capabilities_stable_across_calls() -> None:
    backend = AcpBackend("grok")
    assert backend.capabilities() == backend.capabilities()


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    with pytest.raises(RuntimeError, match="grok"):
        AcpBackend("grok").preflight(SessionSpec(cwd="/tmp"))


def test_create_session_and_dispose() -> None:
    backend = AcpBackend("grok")
    session = backend.create_session(SessionSpec(cwd="/tmp"))
    assert isinstance(session, AcpSession)
    assert session.session_id.startswith("grok-")
    backend.dispose()


# -- resume probe -------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_resume_without_resume_id_returns_resumed() -> None:
    session = AcpSession(SessionSpec(cwd="/tmp"), _test_runtime())
    try:
        assert await session.probe_resume() is ResumeStatus.RESUMED
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_probe_resume_with_resume_id_is_undetectable() -> None:
    spec = SessionSpec(cwd="/tmp", resume_session_id="resume-1")
    session = AcpSession(spec, _test_runtime())
    try:
        assert await session.probe_resume() is ResumeStatus.UNDETECTABLE
    finally:
        await session.close()


# -- integration round-trips -------------------------------------------

_OnEvent = Callable[[EventEnvelope], Awaitable[None]]


async def _run_send_and_collect(
    session: AcpSession,
    on_event: _OnEvent | None = None,
    timeout: float = 5.0,
) -> list[EventEnvelope]:
    """Run ``send()`` in the background and collect events until it finishes.

    ``on_event`` is invoked per event while ``send()`` is still in flight so
    the consumer can drive approve()/interrupt() round-trips mid-turn.
    """
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
async def test_send_translates_event_stream_in_order() -> None:
    session = AcpSession(SessionSpec(cwd="/tmp"), _test_runtime("immediate"))
    try:
        await session.send("hello")
        events = [ev async for ev in session.events()]
    finally:
        await session.close()

    assert [ev.kind for ev in events] == [
        EventKind.TEXT_DELTA,
        EventKind.TEXT_DELTA,
        EventKind.TOOL_CALL,
        EventKind.APPROVAL_REQUEST,
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    assert events[0].payload["text"] == "hello "
    assert events[1].payload["text"] == "world"
    assert events[2].payload["name"] == "Bash"
    assert events[3].payload["tool_name"] == "Bash"
    assert events[4].payload == {"reason": "success"}
    assert events[5].payload == {"reason": "success"}


@pytest.mark.asyncio
async def test_approve_round_trip_emits_permission_granted() -> None:
    session = AcpSession(SessionSpec(cwd="/tmp"), _test_runtime("approval"))
    approved = False

    async def on_event(ev: EventEnvelope) -> None:
        nonlocal approved
        if ev.kind is EventKind.APPROVAL_REQUEST and not approved:
            approved = True
            await session.approve(ev.payload["request_id"], ApprovalDecision.ALLOW)

    try:
        events = await _run_send_and_collect(session, on_event)
    finally:
        await session.close()

    assert approved
    assert events[-1].kind is EventKind.SESSION_COMPLETE

    approval_idx = next(
        i for i, ev in enumerate(events) if ev.kind is EventKind.APPROVAL_REQUEST
    )
    granted_idx = next(
        i
        for i, ev in enumerate(events)
        if ev.kind is EventKind.TEXT_DELTA
        and ev.payload.get("text") == "permission-granted"
    )
    turn_idx = next(
        i for i, ev in enumerate(events) if ev.kind is EventKind.TURN_COMPLETE
    )
    assert approval_idx < granted_idx < turn_idx


@pytest.mark.asyncio
async def test_interrupt_round_trip_emits_cancel_received() -> None:
    session = AcpSession(SessionSpec(cwd="/tmp"), _test_runtime("cancel"))
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
    assert turn.payload["reason"] == "cancel"
