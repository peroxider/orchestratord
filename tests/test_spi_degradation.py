"""Tests for SPI degradation: TEXT → pseudo TEXT_DELTA conversion."""

from __future__ import annotations

import time
from typing import Any

import pytest

from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.degradation import (
    DegradingBackend,
    DegradingSession,
    _split_chunks,
)
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus
from orchestratord.spi.session import ResumeStatus


# ---------------------------------------------------------------------------
# _split_chunks
# ---------------------------------------------------------------------------


def test_split_chunks_empty():
    assert _split_chunks("") == []
    assert _split_chunks("   ") == []


def test_split_chunks_short():
    assert _split_chunks("hello") == ["hello"]


def test_split_chunks_newline_split():
    result = _split_chunks("hello\nworld")
    assert len(result) >= 2
    assert "".join(result).replace("\n", "") == "helloworld"


def test_split_chunks_long_line_hard_cut():
    text = "x" * 1000
    result = _split_chunks(text, chunk_size=200)
    assert len(result) == 5
    assert "".join(result) == text


# ---------------------------------------------------------------------------
# DegradingSession — mock inner session
# ---------------------------------------------------------------------------


class _MockInnerSession:
    """Fake AgentSession that yields a pre-defined list of EventEnvelope."""

    def __init__(
        self,
        events: list[EventEnvelope],
        *,
        probe_result: ResumeStatus = ResumeStatus.UNDETECTABLE,
        has_probe: bool = True,
    ) -> None:
        self._events = events
        self.session_id = "mock-session"
        self.capabilities = BackendCapabilities(streaming_deltas=False)
        self._probe_result = probe_result
        self._has_probe = has_probe

    async def probe_resume(self) -> ResumeStatus:
        if not self._has_probe:
            raise AttributeError("probe_resume not implemented")
        return self._probe_result

    async def send(self, content):
        pass

    async def interrupt(self):
        pass

    async def approve(self, request_id, decision):
        pass

    async def close(self):
        pass

    async def events(self):
        for env in self._events:
            yield env


def _make_env(seq: int, kind: EventKind, payload: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope(seq=seq, timestamp=time.time(), kind=kind, payload=payload)


async def _collect(session) -> list[EventEnvelope]:
    return [env async for env in session.events()]


# ---------------------------------------------------------------------------
# Test: TEXT → TEXT_DELTA split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_split_into_deltas():
    inner = _MockInnerSession(
        [
            _make_env(1, EventKind.TEXT, {"text": "hello\nworld"}),
        ]
    )
    session = DegradingSession(inner)
    events = await _collect(session)
    assert len(events) >= 2
    for e in events:
        assert e.kind is EventKind.TEXT_DELTA
    full = "".join(e.payload["text"] for e in events)
    assert "hello" in full
    assert "world" in full


@pytest.mark.asyncio
async def test_text_empty_payload_skipped():
    inner = _MockInnerSession(
        [
            _make_env(1, EventKind.TEXT, {"text": ""}),
        ]
    )
    session = DegradingSession(inner)
    events = await _collect(session)
    assert len(events) == 0


# ---------------------------------------------------------------------------
# Test: seq monotonic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seq_monotonic_across_split():
    inner = _MockInnerSession(
        [
            _make_env(1, EventKind.TEXT, {"text": "hello\nworld"}),
            _make_env(2, EventKind.TOOL_CALL, {"tool_name": "grep"}),
        ]
    )
    session = DegradingSession(inner)
    events = await _collect(session)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


# ---------------------------------------------------------------------------
# Test: pure TEXT_DELTA pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pure_text_delta_passthrough():
    inner = _MockInnerSession(
        [
            _make_env(1, EventKind.TEXT_DELTA, {"text": "chunk1", "delta": "chunk1"}),
            _make_env(2, EventKind.TEXT_DELTA, {"text": "chunk2", "delta": "chunk2"}),
            _make_env(3, EventKind.TOOL_CALL, {"tool_name": "grep"}),
        ]
    )
    session = DegradingSession(inner)
    events = await _collect(session)
    assert len(events) == 3
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.TEXT_DELTA, EventKind.TEXT_DELTA, EventKind.TOOL_CALL]
    assert events[0].payload["text"] == "chunk1"
    assert events[1].payload["text"] == "chunk2"


# ---------------------------------------------------------------------------
# Test: no TEXT in output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_text_kind_in_output():
    inner = _MockInnerSession(
        [
            _make_env(1, EventKind.TEXT, {"text": "hello"}),
            _make_env(2, EventKind.TEXT_DELTA, {"text": "world", "delta": "world"}),
            _make_env(3, EventKind.TOOL_CALL, {"tool_name": "grep"}),
        ]
    )
    session = DegradingSession(inner)
    events = await _collect(session)
    for e in events:
        assert e.kind is not EventKind.TEXT


# ---------------------------------------------------------------------------
# Test: DegradingBackend wrapper
# ---------------------------------------------------------------------------


class _MockBackend:
    name = "mock"
    display_name = "Mock Backend"

    def capabilities(self):
        return BackendCapabilities(streaming_deltas=False)

    def create_session(self, spec):
        return _MockInnerSession(
            [_make_env(1, EventKind.TEXT, {"text": "hello"})]
        )

    def dispose(self):
        pass


@pytest.mark.asyncio
async def test_degrading_backend_wraps_session():
    backend = DegradingBackend(_MockBackend())
    assert backend.name == "mock"
    assert backend.display_name == "Mock Backend"
    caps = backend.capabilities()
    assert caps.streaming_deltas is False

    session = backend.create_session(None)
    events = await _collect(session)
    assert len(events) == 1
    assert events[0].kind is EventKind.TEXT_DELTA
    assert events[0].payload["text"] == "hello"


# ---------------------------------------------------------------------------
# Test: single long line hard-cut
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_long_line_hard_cut():
    long_text = "A" * 1000
    inner = _MockInnerSession(
        [_make_env(1, EventKind.TEXT, {"text": long_text})]
    )
    session = DegradingSession(inner)
    events = await _collect(session)
    assert len(events) == 5
    reconstructed = "".join(e.payload["text"] for e in events)
    assert reconstructed == long_text


# ---------------------------------------------------------------------------
# Test: probe_resume forwarding across DegradingSession
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degrading_session_forwards_probe_resume_resumed():
    inner = _MockInnerSession(
        [_make_env(1, EventKind.TEXT_DELTA, {"text": "x", "delta": "x"})],
        probe_result=ResumeStatus.RESUMED,
    )
    wrapped = DegradingSession(inner)
    assert await wrapped.probe_resume() is ResumeStatus.RESUMED


@pytest.mark.asyncio
async def test_degrading_session_forwards_probe_resume_rejected():
    inner = _MockInnerSession(
        [_make_env(1, EventKind.TEXT_DELTA, {"text": "x", "delta": "x"})],
        probe_result=ResumeStatus.REJECTED,
    )
    wrapped = DegradingSession(inner)
    assert await wrapped.probe_resume() is ResumeStatus.REJECTED


@pytest.mark.asyncio
async def test_degrading_session_forwards_probe_resume_undetectable():
    inner = _MockInnerSession(
        [_make_env(1, EventKind.TEXT_DELTA, {"text": "x", "delta": "x"})],
        probe_result=ResumeStatus.UNDETECTABLE,
    )
    wrapped = DegradingSession(inner)
    assert await wrapped.probe_resume() is ResumeStatus.UNDETECTABLE


@pytest.mark.asyncio
async def test_degrading_session_handles_missing_probe_resume():
    """An inner session without ``probe_resume`` must yield UNDETECTABLE.

    The SPI requires every session — including legacy ones — to expose
    a three-state ``probe_resume()`` so the orchestrator can
    distinguish "no opinion" from "definitely resumed".
    ``DegradingSession`` enforces this contract for the caller.
    """

    class _LegacyNoProbeSession:
        session_id = "legacy-session"
        capabilities = BackendCapabilities(streaming_deltas=False)

        async def send(self, content):
            return None

        async def interrupt(self):
            return None

        async def approve(self, request_id, decision):
            return None

        async def close(self):
            return None

        async def events(self):
            if False:  # pragma: no cover — makes this an async generator
                yield EventEnvelope(
                    seq=1, timestamp=0.0,
                    kind=EventKind.SESSION_COMPLETE, payload={"reason": "ok"},
                )

    wrapped = DegradingSession(_LegacyNoProbeSession())
    assert await wrapped.probe_resume() is ResumeStatus.UNDETECTABLE


def test_current_pid_forwarded_from_inner() -> None:
    """The live-session registry resolves operator pause/stop through
    ``current_pid``; the wrapper must forward it or per-turn backends
    (e.g. ``claude -p``) become invisible to §5.2.3 forwarding.
    """

    class _InnerWithPid:
        session_id = "inner-pid"
        capabilities = BackendCapabilities()
        current_pid = 4242

    assert DegradingSession(_InnerWithPid()).current_pid == 4242


def test_current_pid_none_without_inner_support() -> None:
    class _InnerWithoutPid:
        session_id = "inner-no-pid"
        capabilities = BackendCapabilities()

    assert DegradingSession(_InnerWithoutPid()).current_pid is None