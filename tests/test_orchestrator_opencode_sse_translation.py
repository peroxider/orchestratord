"""Tests for Scheme B: opencode SSE translation.

Verifies that:

1. ``OpenCodeSession._parse_sse_line`` correctly extracts JSON payloads
   from ``data: {...}`` frames (with and without a leading space) and
   discards comments / blanks / non-data lines.

2. ``OpenCodeSession.send`` translates SSE frames into the SPI
   ``EventKind`` set (``TEXT_DELTA``, ``TOOL_CALL``, ``TOOL_RESULT``,
   ``TURN_COMPLETE``, ``SESSION_COMPLETE``, ``ERROR``) with
   monotonically-increasing ``seq`` values.

3. ``OpenCodeSession.send`` falls back to a plain ``TEXT`` event when
   the server returns a non-200 status (the "old opencode" failure
   path described in Scheme B §2.6).

4. ``OpenCodeSession.send`` translates a stream-time exception into
   ``EventKind.ERROR`` and emits terminal events with ``reason="error"``.

5. ``OpenCodeSession.approve`` POSTs the decision to
   ``/v1/approvals/{request_id}`` with the right body shape.

6. ``_ensure_server`` raises a clear error when the port-discovery
   timeout elapses.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind

from orchestratord_opencode.session import OpenCodeSession


# ---------------------------------------------------------------------------
# SSE line parser
# ---------------------------------------------------------------------------


def test_parse_sse_line_data_with_space() -> None:
    line = 'data: {"event": "message.delta", "text": "hi"}'
    out = OpenCodeSession._parse_sse_line(line)  # noqa: SLF001 — direct unit test
    assert out == {"event": "message.delta", "text": "hi"}


def test_parse_sse_line_data_no_space() -> None:
    line = 'data:{"event":"tool.call","call_id":"c1","name":"ls"}'
    out = OpenCodeSession._parse_sse_line(line)
    assert out == {"event": "tool.call", "call_id": "c1", "name": "ls"}


def test_parse_sse_line_blank_returns_none() -> None:
    assert OpenCodeSession._parse_sse_line("") is None
    assert OpenCodeSession._parse_sse_line("   ") is None


def test_parse_sse_line_comment_returns_none() -> None:
    assert OpenCodeSession._parse_sse_line(": keep-alive") is None


def test_parse_sse_line_non_data_returns_none() -> None:
    """``event:`` lines without a matching ``data:`` are ignored."""
    assert OpenCodeSession._parse_sse_line("event: ping") is None


def test_parse_sse_line_non_json_falls_back_to_text_delta() -> None:
    """A ``data:`` line that is not valid JSON yields a synthetic
    ``message.delta`` payload so the consumer still sees the bytes.
    """
    out = OpenCodeSession._parse_sse_line("data: just plain text")
    assert out == {
        "event": "message.delta",
        "text": "just plain text",
        "delta": "just plain text",
    }


# ---------------------------------------------------------------------------
# send() translation
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for ``httpx.Response`` used inside ``client.stream``."""

    def __init__(
        self,
        status_code: int,
        lines: list[str] | None = None,
        body: bytes | None = None,
        content_type: str = "text/event-stream",
    ) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._body = body or b""
        self.headers = {"content-type": content_type}

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class _FakeStreamClient:
    """Records the call to ``client.stream`` and returns a canned response."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append((method, url, json or {}))
        return self._response


def _spec() -> SessionSpec:
    return SessionSpec(cwd="/tmp")


def _session_with_fake_server(
    client: _FakeStreamClient, port: int = 41234
) -> OpenCodeSession:
    """Build a session whose ``_ensure_server`` is replaced with a
    constant — so we don't actually spawn ``opencode serve``.
    """
    s = OpenCodeSession(_spec())

    async def _fake_ensure() -> tuple[int, httpx.AsyncClient]:
        return port, client  # type: ignore[return-value]

    s._ensure_server = _fake_ensure  # type: ignore[method-assign]
    return s


def _drain(session: OpenCodeSession) -> list[Any]:
    out: list[Any] = []

    async def collect() -> None:
        async for ev in session.events():
            out.append(ev)

    asyncio.run(collect())
    return out


def test_send_translates_text_delta_tool_call_tool_result() -> None:
    """A stream emitting message.delta × 2, tool.call, tool.result
    produces TEXT_DELTA ×2, TOOL_CALL, TOOL_RESULT in order.
    """
    lines = [
        f"data: {json.dumps({'event': 'message.delta', 'text': 'Hello'})}",
        f"data: {json.dumps({'event': 'message.delta', 'text': ' world'})}",
        f"data: {json.dumps({'event': 'tool.call', 'call_id': 'c1', 'name': 'ls', 'arguments': {'path': '.'}})}",
        f"data: {json.dumps({'event': 'tool.result', 'call_id': 'c1', 'output': 'README.md'})}",
        f"data: {json.dumps({'event': 'turn.complete', 'reason': 'success'})}",
    ]
    fake = _FakeStreamClient(_FakeResponse(200, lines=lines))
    session = _session_with_fake_server(fake)

    asyncio.run(session.send("hi"))

    events = _drain(session)
    kinds = [e.kind for e in events]
    assert kinds == [
        EventKind.TEXT_DELTA,
        EventKind.TEXT_DELTA,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TURN_COMPLETE,
        EventKind.TURN_COMPLETE,  # send()'s own terminal TURN_COMPLETE
        EventKind.SESSION_COMPLETE,
    ]

    # seq is strictly monotonic
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)

    # payload shapes
    assert events[0].payload["text"] == "Hello"
    assert events[0].payload["delta"] == "Hello"
    assert events[2].payload["name"] == "ls"
    assert events[2].payload["arguments"] == {"path": "."}
    assert events[3].payload["call_id"] == "c1"
    assert events[3].payload["output"] == "README.md"
    assert events[3].payload["ok"] is True

    # terminal SESSION_COMPLETE comes from send()'s finally — reason is
    # "success" because no error was emitted.
    assert events[-1].payload == {"reason": "success"}


def test_send_falls_back_to_text_on_non_200() -> None:
    """A 200 response whose content-type is not ``text/event-stream``
    (the historical opencode binary) degrades to a single ``TEXT``
    event instead of being mis-parsed as SSE.
    """
    fake = _FakeStreamClient(
        _FakeResponse(
            200,
            body=b"the model said: hi",
            content_type="application/json",
        ),
    )
    session = _session_with_fake_server(fake)

    asyncio.run(session.send("hi"))

    events = _drain(session)
    text_events = [e for e in events if e.kind == EventKind.TEXT]
    assert len(text_events) == 1
    assert text_events[0].payload["text"] == "the model said: hi"
    # terminal events still emitted
    assert events[-1].kind == EventKind.SESSION_COMPLETE


def test_send_falls_back_on_http_error() -> None:
    """A 500 from the opencode server falls back to a TEXT event with
    the raw body bytes.
    """
    fake = _FakeStreamClient(
        _FakeResponse(500, body=b"upstream gateway timeout"),
    )
    session = _session_with_fake_server(fake)

    asyncio.run(session.send("hi"))

    events = _drain(session)
    text_events = [e for e in events if e.kind == EventKind.TEXT]
    assert len(text_events) == 1
    assert text_events[0].payload["text"] == "upstream gateway timeout"
    # The terminal SESSION_COMPLETE keeps reason="success" because the
    # fallback is a soft downgrade, not a hard error.
    assert events[-1].payload == {"reason": "success"}


def test_send_emits_error_when_stream_raises() -> None:
    """If the underlying httpx call raises, the session translates it
    into EventKind.ERROR and tags the terminal as reason="error".
    """

    class _ExplodingClient(_FakeStreamClient):
        def stream(
            self,
            method: str,
            url: str,
            *,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            timeout: float | None = None,
        ) -> _FakeResponse:
            raise httpx.RemoteProtocolError("server closed connection")

    session = _session_with_fake_server(_ExplodingClient(_FakeResponse(200)))

    asyncio.run(session.send("hi"))

    events = _drain(session)
    err = [e for e in events if e.kind == EventKind.ERROR]
    assert len(err) == 1
    assert err[0].payload["code"] == "opencode_error"
    assert "RemoteProtocolError" in err[0].payload["message"]
    assert events[-1].kind == EventKind.SESSION_COMPLETE
    assert events[-1].payload == {"reason": "error"}


# ---------------------------------------------------------------------------
# approve() forwarding
# ---------------------------------------------------------------------------


class _RecorderClient(_FakeStreamClient):
    """A ``_FakeStreamClient`` that also records POST calls for ``approve()``."""

    def __init__(self, response: _FakeResponse) -> None:
        super().__init__(response)
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None) -> Any:
        self.posts.append((url, json or {}))
        return _FakeResponse(200, body=b"")


def test_approve_posts_decision_to_server() -> None:
    """An approval.request frame followed by ``approve(req_id, ALLOW)``
    POSTs ``{"decision": "allow", "call_id": "..."}`` to
    ``/v1/approvals/{req_id}``.
    """
    lines = [
        f"data: {json.dumps({'event': 'approval.request', 'request_id': 'r1', 'call_id': 'c1', 'tool_name': 'write_file', 'arguments': {'path': '/x'}})}",
    ]
    recorder_client = _RecorderClient(_FakeResponse(200, lines=lines))

    s = OpenCodeSession(_spec())

    async def fake_ensure() -> tuple[int, Any]:
        # The real _ensure_server writes self._client; replicate that so
        # approve() can find the recorder client.
        s._client = recorder_client  # type: ignore[assignment]
        return 41234, recorder_client

    s._ensure_server = fake_ensure  # type: ignore[method-assign]

    asyncio.run(s.send("hi"))
    asyncio.run(s.approve("r1", ApprovalDecision.ALLOW))

    assert recorder_client.posts == [
        ("/v1/approvals/r1", {"decision": "allow", "call_id": "c1"}),
    ]
    # The pending approval was consumed
    assert "r1" not in s._pending_approvals


def test_approve_unknown_request_id_is_noop() -> None:
    """Approving a request_id that was never cached is silently ignored."""
    s = OpenCodeSession(_spec())
    asyncio.run(s.approve("never-seen", ApprovalDecision.DENY))
    assert s._pending_approvals == {}


# ---------------------------------------------------------------------------
# _ensure_server timeout
# ---------------------------------------------------------------------------


class _SilentStderr:
    """An stderr-like object whose ``readline`` always returns ``b""``."""

    async def readline(self) -> bytes:  # noqa: D401 - test stub
        return b""


class _SilentProcess:
    stderr = _SilentStderr()
    stdout = _SilentStderr()

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        return 0


def test_ensure_server_raises_when_no_port_seen() -> None:
    """If the opencode subprocess never prints a port line, the session
    raises RuntimeError after the configured timeout.
    """
    import orchestratord_opencode.session as session_mod

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        return _SilentProcess()

    s = OpenCodeSession(_spec())

    original_exec = session_mod.asyncio.create_subprocess_exec
    session_mod.asyncio.create_subprocess_exec = fake_exec  # type: ignore[assignment]
    # Tighten timeouts so the test is fast
    session_mod._SERVER_READY_TIMEOUT_SECONDS = 0.2
    session_mod._LINE_TIMEOUT_SECONDS = 0.05
    try:
        with pytest.raises(RuntimeError, match="could not discover opencode serve port"):
            asyncio.run(s._ensure_server())  # noqa: SLF001
    finally:
        session_mod.asyncio.create_subprocess_exec = original_exec  # type: ignore[assignment]
        session_mod._SERVER_READY_TIMEOUT_SECONDS = 10.0
        session_mod._LINE_TIMEOUT_SECONDS = 1.0