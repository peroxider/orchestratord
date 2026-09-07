"""Tests for the opencode backend transport rewrite.

Verifies the real opencode HTTP protocol integration:

1. ``send()`` is non-blocking (dsh pattern) — it returns before the
   turn completes; events flow through the queue into ``events()``.
2. The real ``/api`` protocol is used: session create, prompt submit,
   global-bus SSE consumption — never ``/v1/chat``.
3. Frame → SPI translation (deltas, tool calls/results, step endings,
   usage accumulation) with session-ID filtering for the shared bus.
4. Approval flow: ``permission.v2.asked`` → APPROVAL_REQUEST envelope;
   ``approve()`` posts once/always/reject to the reply endpoint.
5. Failure honesty: non-SSE bus responses, session-create and prompt
   rejections all produce ERROR envelopes with terminal
   ``reason="error"`` (no fake-success fallback path).
6. ``probe_resume``: RESUMED / REJECTED / UNDETECTABLE including the
   non-``ses_*`` id shape check.
7. Port discovery from the serve **stdout** banner with no process
   leak on failure, and ``spec.env`` passthrough to the subprocess.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self

import orchestratord_opencode.session as session_mod
import pytest
from orchestratord_opencode.session import OpenCodeSession, _TransportError

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind
from orchestratord.spi.session import ResumeStatus

OC_SID = "ses_test1234567890"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    """Non-stream response for client.post/get."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return self._json


class _FakeStreamResp:
    """SSE stream response for client.stream."""

    def __init__(
        self,
        lines: list[str],
        status_code: int = 200,
        content_type: str = "text/event-stream",
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._lines = lines

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    """Records post/get calls; serves a scripted SSE stream."""

    def __init__(
        self,
        stream_lines: list[str] | None = None,
        stream_status: int = 200,
        stream_content_type: str = "text/event-stream",
        post_responses: dict[str, _FakeResp] | None = None,
        get_responses: dict[str, _FakeResp] | None = None,
    ) -> None:
        self.posts: list[tuple[str, dict[str, Any] | None]] = []
        self.gets: list[str] = []
        self.stream_calls: list[tuple[str, str]] = []
        self._stream_lines = stream_lines or []
        self._stream_status = stream_status
        self._stream_content_type = stream_content_type
        self._post_responses = post_responses or {}
        self._get_responses = get_responses or {}

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
    ) -> _FakeStreamResp:
        self.stream_calls.append((method, url))
        return _FakeStreamResp(
            self._stream_lines, self._stream_status, self._stream_content_type
        )

    async def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        timeout: Any = None,
    ) -> _FakeResp:
        self.posts.append((url, json))
        for prefix, resp in self._post_responses.items():
            if url.startswith(prefix):
                return resp
        return _FakeResp(200, {"data": {"id": OC_SID}})

    async def get(self, url: str, timeout: Any = None) -> _FakeResp:
        self.gets.append(url)
        for prefix, resp in self._get_responses.items():
            if url.startswith(prefix):
                return resp
        return _FakeResp(200, {"data": {"id": OC_SID}})

    async def aclose(self) -> None:
        return None


def _frame(f_type: str, **data: Any) -> str:
    """One SSE bus frame in opencode wire shape."""
    return "data: " + json.dumps(
        {
            "id": "evt_x",
            "type": f_type,
            "durable": {"aggregateID": data.get("sessionID", OC_SID), "seq": 1},
            "data": data,
        }
    )


def _turn_lines(
    *,
    session_id: str = OC_SID,
    text_deltas: bool = True,
    finish: str = "stop",
) -> list[str]:
    """A complete minimal turn: text + tool call + result + terminal step."""
    lines = [
        _frame("session.next.prompt.admitted", sessionID=session_id),
        _frame("session.next.step.started", sessionID=session_id),
    ]
    if text_deltas:
        lines.append(
            _frame(
                "session.next.text.delta",
                sessionID=session_id,
                textID="text-0",
                delta="Hello",
            )
        )
        lines.append(
            _frame(
                "session.next.text.ended",
                sessionID=session_id,
                textID="text-0",
                text="Hello world",
            )
        )
    else:
        lines.append(
            _frame(
                "session.next.text.ended",
                sessionID=session_id,
                textID="text-0",
                text="Hello world",
            )
        )
    lines.extend(
        [
            _frame(
                "session.next.tool.called",
                sessionID=session_id,
                callID="call_1",
                tool="write",
                input={"path": "a.txt", "content": "x"},
            ),
            _frame(
                "session.next.tool.success",
                sessionID=session_id,
                callID="call_1",
                content=[{"type": "text", "text": "Created file"}],
            ),
            _frame(
                "session.next.step.ended",
                sessionID=session_id,
                finish=finish,
                tokens={"input": 10, "output": 5, "reasoning": 1,
                        "cache": {"read": 7, "write": 0}},
            ),
        ]
    )
    return lines


def _session(
    client: _FakeClient,
    spec: SessionSpec | None = None,
) -> OpenCodeSession:
    """Build a session whose server bootstrap is replaced by the fake."""
    s = OpenCodeSession(
        spec if spec is not None else SessionSpec(cwd="/tmp"),
        client_factory=lambda port: client,
    )

    async def _fake_ensure() -> Any:
        s._client = client
        s._port = 45551
        return client

    s._ensure_server = _fake_ensure  # type: ignore[method-assign]
    return s


def _send_and_drain(
    session: OpenCodeSession, prompt: str = "hi"
) -> list[Any]:
    """send() + full event drain in one loop (mirrors the core timing)."""

    async def run() -> list[Any]:
        await session.send(prompt)
        out: list[Any] = []
        async for ev in session.events():
            out.append(ev)
        return out

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Non-blocking send + basic translation
# ---------------------------------------------------------------------------


def test_send_is_nonblocking_and_events_flow() -> None:
    """send() returns while the turn is still pending; the drain then
    collects the full event set — proving the queue bridge works.
    """
    gate = asyncio.Event()

    class _GatedClient(_FakeClient):
        def stream(self, method, url, *, headers=None, timeout=None):
            self.stream_calls.append((method, url))
            return _GatedStreamResp(self._stream_lines)

    class _GatedStreamResp(_FakeStreamResp):
        async def aiter_lines(self):
            await gate.wait()
            for line in self._lines:
                yield line

    client = _GatedClient(_turn_lines())
    session = _session(client)

    async def run() -> list[Any]:
        await session.send("hi")
        # send() returned while the turn task is blocked on the gate —
        # the whole turn is still pending. Deterministic non-blocking proof.
        assert session._turn_task is not None
        assert not session._turn_task.done()
        gate.set()
        out: list[Any] = []
        async for ev in session.events():
            out.append(ev)
        return out

    events = asyncio.run(run())
    kinds = [e.kind for e in events]
    assert kinds == [
        EventKind.TEXT_DELTA,  # liveness marker on prompt.admitted
        EventKind.TEXT_DELTA,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    # Liveness marker is empty — the core skips it in output accumulation.
    assert events[0].payload == {"text": "", "delta": ""}
    # Real protocol endpoints only: global bus stream + session create + prompt.
    assert client.stream_calls == [("GET", "/api/event")]
    assert any(u == "/api/session" for u, _ in client.posts)
    assert any(
        u == f"/api/session/{OC_SID}/prompt" for u, _ in client.posts
    )


def test_translation_payload_shapes() -> None:
    client = _FakeClient(_turn_lines())
    session = _session(client)
    events = _send_and_drain(session)

    assert events[0].payload == {"text": "", "delta": ""}  # liveness
    delta = events[1]
    assert delta.payload == {"text": "Hello", "delta": "Hello"}
    tool_call = events[2]
    assert tool_call.payload["call_id"] == "call_1"
    assert tool_call.payload["name"] == "write"
    assert tool_call.payload["arguments"] == {"path": "a.txt", "content": "x"}
    tool_result = events[3]
    assert tool_result.payload["ok"] is True
    assert tool_result.payload["output"] == "Created file"
    turn_complete = events[4]
    assert turn_complete.payload["reason"] == "success"
    assert turn_complete.payload["finish"] == "stop"
    # Terminal pair keeps core expectations.
    assert events[-1].payload["reason"] == "success"
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_text_ended_without_deltas_emits_text_once() -> None:
    """Coalesced answers (no delta frames) degrade to one TEXT event."""
    client = _FakeClient(_turn_lines(text_deltas=False))
    session = _session(client)
    events = _send_and_drain(session)
    kinds = [e.kind for e in events]
    assert EventKind.TEXT in kinds
    # No content-bearing TEXT_DELTA (the only one is the empty liveness
    # marker emitted on prompt.admitted).
    assert not [
        e for e in events
        if e.kind is EventKind.TEXT_DELTA and e.payload.get("text")
    ]
    text_events = [e for e in events if e.kind is EventKind.TEXT]
    assert len(text_events) == 1
    assert text_events[0].payload["text"] == "Hello world"


def test_reasoning_delta_streams_as_text_delta() -> None:
    """dsh parity: reasoning deltas surface as TEXT_DELTA so the core's
    inactivity watchdog stays fed during long thinking phases.
    """
    lines = [
        _frame("session.next.prompt.admitted", sessionID=OC_SID),
        _frame(
            "session.next.reasoning.delta",
            sessionID=OC_SID,
            reasoningID="reasoning-0",
            delta="thinking…",
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    deltas = [e for e in events if e.kind is EventKind.TEXT_DELTA]
    assert any(e.payload["delta"] == "thinking…" for e in deltas)


def test_frames_from_other_sessions_are_filtered() -> None:
    lines = [
        _frame(
            "session.next.text.delta",
            sessionID="ses_other999999999",
            textID="text-0",
            delta="foreign",
        ),
        _frame(
            "session.next.step.ended",
            sessionID="ses_other999999999",
            finish="stop",
            tokens={},
        ),
        *_turn_lines(),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    texts = [
        e.payload.get("text", "")
        for e in events
        if e.kind is EventKind.TEXT_DELTA
    ]
    assert "foreign" not in texts
    assert "Hello" in texts


def test_tool_calls_finish_uses_tool_calls() -> None:
    """finish=tool-calls does NOT end the turn — the next step does."""
    lines = [
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="tool-calls",
            tokens={},
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    turns = [e for e in events if e.kind is EventKind.TURN_COMPLETE]
    assert len(turns) == 1


def test_step_failed_is_not_terminal() -> None:
    lines = [
        _frame(
            "session.next.step.failed",
            sessionID=OC_SID,
            error={"type": "unknown", "message": "429 rate limited"},
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "opencode_step_failed"
    assert "429" in errors[0].payload["message"]
    # The turn still completed normally after the failed step.
    assert events[-1].payload["reason"] == "success"


def test_usage_accumulates_into_session_complete() -> None:
    lines = _turn_lines()
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    usage = events[-1].payload.get("usage")
    assert usage == {
        "input": 10,
        "output": 5,
        "reasoning": 1,
        "cache_read": 7,
        "cache_write": 0,
    }


# ---------------------------------------------------------------------------
# Model ref parsing
# ---------------------------------------------------------------------------


def test_model_ref_parsing() -> None:
    spec = SessionSpec(cwd="/tmp", model="火山AI网关/deepseek-v4-flash")
    s = OpenCodeSession(spec)
    assert s._model_ref() == {
        "providerID": "火山AI网关",
        "id": "deepseek-v4-flash",
    }
    # No slash → refuse to guess (server default + warning).
    s2 = OpenCodeSession(SessionSpec(cwd="/tmp", model="deepseek-v4-flash"))
    assert s2._model_ref() is None
    # Empty → no model at all.
    s3 = OpenCodeSession(SessionSpec(cwd="/tmp"))
    assert s3._model_ref() is None


def test_model_ref_reaches_session_create_body() -> None:
    client = _FakeClient(_turn_lines())
    spec = SessionSpec(cwd="/tmp", model="火山AI网关/deepseek-v4-flash")
    session = _session(client, spec=spec)
    _send_and_drain(session)
    create_bodies = [
        body for url, body in client.posts if url == "/api/session"
    ]
    assert create_bodies == [
        {"model": {"providerID": "火山AI网关", "id": "deepseek-v4-flash"}}
    ]


def test_session_reused_across_turns() -> None:
    client = _FakeClient(_turn_lines())
    session = _session(client)

    # The queue binds to the running loop (dsh parity): both turns must
    # run — and be drained — in one loop, exactly like the core does.
    async def run() -> list[Any]:
        all_events: list[Any] = []
        for prompt in ("first prompt", "second prompt"):
            await session.send(prompt)
            async for ev in session.events():
                all_events.append(ev)
        return all_events

    events = asyncio.run(run())
    creates = [u for u, _ in client.posts if u == "/api/session"]
    assert len(creates) == 1
    prompts = [u for u, _ in client.posts if u.endswith("/prompt")]
    assert len(prompts) == 2
    # Two turns → two terminal pairs.
    completes = [e for e in events if e.kind is EventKind.SESSION_COMPLETE]
    assert len(completes) == 2


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------


def _approval_lines(session_id: str = OC_SID) -> list[str]:
    return [
        _frame(
            "session.next.tool.called",
            sessionID=session_id,
            callID="call_1",
            tool="bash",
            input={"command": "pwd"},
        ),
        _frame(
            "permission.v2.asked",
            sessionID=session_id,
            id="per_req1",
            action="bash",
            resources=["pwd"],
            source={
                "type": "tool",
                "messageID": "msg_1",
                "callID": "call_1",
            },
        ),
        _frame(
            "session.next.step.ended",
            sessionID=session_id,
            finish="stop",
            tokens={},
        ),
    ]


def test_permission_asked_emits_approval_request() -> None:
    client = _FakeClient(_approval_lines())
    session = _session(client)
    events = _send_and_drain(session)
    approvals = [e for e in events if e.kind is EventKind.APPROVAL_REQUEST]
    assert len(approvals) == 1
    payload = approvals[0].payload
    assert payload["request_id"] == "per_req1"
    assert payload["call_id"] == "call_1"
    assert payload["tool_name"] == "bash"
    assert payload["arguments"] == {"command": "pwd"}
    assert "per_req1" in session._pending_approvals


def test_permission_asked_foreign_session_filtered() -> None:
    client = _FakeClient(_approval_lines(session_id="ses_other999999999"))
    session = _session(client)
    events = _send_and_drain(session)
    assert not [e for e in events if e.kind is EventKind.APPROVAL_REQUEST]
    assert session._pending_approvals == {}


def test_approve_posts_wire_reply() -> None:
    client = _FakeClient(_approval_lines())
    session = _session(client)
    _send_and_drain(session)

    asyncio.run(session.approve("per_req1", ApprovalDecision.ALLOW))
    assert client.posts[-1] == (
        f"/api/session/{OC_SID}/permission/per_req1/reply",
        {"reply": "once"},
    )
    assert "per_req1" not in session._pending_approvals

    # always_allow → "always"
    client2 = _FakeClient(_approval_lines())
    session2 = _session(client2)
    _send_and_drain(session2)
    asyncio.run(session2.approve("per_req1", ApprovalDecision.ALWAYS_ALLOW))
    assert client2.posts[-1][1] == {"reply": "always"}

    # deny → "reject"
    client3 = _FakeClient(_approval_lines())
    session3 = _session(client3)
    _send_and_drain(session3)
    asyncio.run(session3.approve("per_req1", ApprovalDecision.DENY))
    assert client3.posts[-1][1] == {"reply": "reject"}


def test_approve_unknown_request_id_is_noop() -> None:
    client = _FakeClient(_turn_lines())
    session = _session(client)
    asyncio.run(session.approve("never-seen", ApprovalDecision.DENY))
    assert client.posts == []


# ---------------------------------------------------------------------------
# Failure honesty (no fake-success path)
# ---------------------------------------------------------------------------


def test_non_sse_bus_response_is_error_not_text() -> None:
    """A 200 response without event-stream content-type must surface as
    ERROR + terminal error — historically this was the fake-success hole
    (the SPA catch-all returned HTML and was reported as success).
    """
    client = _FakeClient(
        ["<html>not sse</html>"], stream_content_type="text/html"
    )
    session = _session(client)
    events = _send_and_drain(session)
    kinds = [e.kind for e in events]
    assert EventKind.ERROR in kinds
    assert EventKind.TEXT not in kinds
    assert EventKind.TEXT_DELTA not in kinds
    assert events[-1].payload["reason"] == "error"


def test_session_create_failure_is_error() -> None:
    client = _FakeClient(
        _turn_lines(),
        post_responses={"/api/session": _FakeResp(500)},
    )
    session = _session(client)
    events = _send_and_drain(session)
    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "opencode_session_create"
    assert events[-1].payload["reason"] == "error"


def test_prompt_rejection_is_error() -> None:
    client = _FakeClient(
        _turn_lines(),
        post_responses={
            f"/api/session/{OC_SID}/prompt": _FakeResp(409)
        },
    )
    session = _session(client)
    events = _send_and_drain(session)
    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "opencode_prompt_rejected"
    assert events[-1].payload["reason"] == "error"


def test_bus_stream_ending_without_terminal_is_error() -> None:
    client = _FakeClient(
        [_frame("session.next.prompt.admitted", sessionID=OC_SID)]
    )
    session = _session(client)
    events = _send_and_drain(session)
    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "opencode_turn_unterminated"
    assert events[-1].payload["reason"] == "error"


# ---------------------------------------------------------------------------
# probe_resume
# ---------------------------------------------------------------------------


def test_probe_resume_no_target_is_resumed() -> None:
    session = _session(_FakeClient())
    assert asyncio.run(session.probe_resume()) is ResumeStatus.RESUMED


def test_probe_resume_non_ses_id_is_undetectable() -> None:
    """Stage/run ids (not ``ses_*``) cannot be probed — and must not
    spawn a server or touch HTTP.
    """
    client = _FakeClient()
    spec = SessionSpec(cwd="/tmp", resume_session_id="stage-01-abcdef12")
    session = _session(client, spec=spec)
    assert asyncio.run(session.probe_resume()) is ResumeStatus.UNDETECTABLE
    assert client.gets == []


def test_probe_resume_ses_id_shapes() -> None:
    spec = SessionSpec(cwd="/tmp", resume_session_id="ses_alive123")
    client = _FakeClient(get_responses={"/api/session/ses_alive123": _FakeResp(200)})
    session = _session(client, spec=spec)
    assert asyncio.run(session.probe_resume()) is ResumeStatus.RESUMED

    client2 = _FakeClient(get_responses={"/api/session/ses_alive123": _FakeResp(404)})
    session2 = _session(client2, spec=spec)
    assert asyncio.run(session2.probe_resume()) is ResumeStatus.REJECTED

    client3 = _FakeClient(get_responses={"/api/session/ses_alive123": _FakeResp(500)})
    session3 = _session(client3, spec=spec)
    assert asyncio.run(session3.probe_resume()) is ResumeStatus.UNDETECTABLE


# ---------------------------------------------------------------------------
# Server lifecycle: port discovery, env, cleanup
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes]) -> None:
        self._lines = list(stdout_lines)
        self.stdout = self
        self.stderr = self
        self.terminated = False
        self.killed = False

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await asyncio.sleep(0.2)  # silence
        return b""

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(
        session_mod.asyncio, "create_subprocess_exec", fake_exec
    )
    return captured


def test_port_discovered_from_stdout_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc(
        [
            b"Warning: OPENCODE_SERVER_PASSWORD is not set; server is unsecured.\n",
            b"opencode server listening on http://127.0.0.1:45551\n",
        ]
    )
    captured = _patch_spawn(monkeypatch, proc)
    seen_ports: list[int | None] = []

    def factory(port: int | None) -> _FakeClient:
        seen_ports.append(port)
        return _FakeClient()

    spec = SessionSpec(cwd="/tmp", env={"OC_SENTINEL": "1"})
    session = OpenCodeSession(spec, client_factory=factory)
    client = asyncio.run(session._ensure_server())
    assert isinstance(client, _FakeClient)
    # The stdout banner is authoritative: the discovered port is the one
    # the banner printed (45551), not merely the one we requested.
    assert session._port == 45551
    assert seen_ports == [45551]
    # Explicit port is passed (never --port 0 — that prefers fixed 4096).
    assert captured["args"][:3] == ("opencode", "serve", "--port")
    assert captured["args"][3] != "0"
    # env passthrough merged with os.environ.
    env = captured["kwargs"]["env"]
    assert env["OC_SENTINEL"] == "1"
    assert "PATH" in env


def test_port_discovery_failure_terminates_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No banner → _TransportError AND the serve process is reaped
    (historically this path leaked orphan serve processes).
    """
    proc = _FakeProc([])  # silence
    _patch_spawn(monkeypatch, proc)
    session = OpenCodeSession(
        SessionSpec(cwd="/tmp"), client_factory=lambda port: _FakeClient()
    )
    # Shrink the discovery deadline for test speed.
    original = session_mod._SERVER_READY_TIMEOUT_SECONDS
    session_mod._SERVER_READY_TIMEOUT_SECONDS = 0.3
    try:
        with pytest.raises(_TransportError, match="port"):
            asyncio.run(session._ensure_server())
    finally:
        session_mod._SERVER_READY_TIMEOUT_SECONDS = original
    assert proc.terminated
    assert session._proc is None


def test_close_terminates_process_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc(
        [b"opencode server listening on http://127.0.0.1:45551\n"]
    )
    _patch_spawn(monkeypatch, proc)
    client = _FakeClient()
    session = OpenCodeSession(
        SessionSpec(cwd="/tmp"), client_factory=lambda port: client
    )
    asyncio.run(session._ensure_server())

    async def run() -> None:
        await session.close()

    asyncio.run(run())
    assert proc.terminated
    assert session._proc is None
    assert session._client is None
    assert session._closed


def test_events_before_send_is_exhausted_stream() -> None:
    session = _session(_FakeClient())

    async def run() -> list[Any]:
        return [e async for e in session.events()]

    assert asyncio.run(run()) == []


def test_interrupt_posts_endpoint() -> None:
    client = _FakeClient(_turn_lines())
    session = _session(client)
    _send_and_drain(session)
    asyncio.run(session.interrupt())
    assert client.posts[-1][0] == f"/api/session/{OC_SID}/interrupt"


# ---------------------------------------------------------------------------
# Model catalog warmup (fresh serve cold-start race)
# ---------------------------------------------------------------------------


def test_first_prompt_waits_for_model_catalog() -> None:
    """A fresh serve's /api/model starts empty; prompting before it
    backfills dies silently (ModelUnavailableError, no bus event). The
    session must poll until the requested model is resolvable.
    """

    class _CatalogClient(_FakeClient):
        def __init__(self, lines):
            super().__init__(lines)
            self.model_polls = 0

        async def get(self, url, timeout=None):
            if url == "/api/model":
                self.model_polls += 1
                if self.model_polls < 2:
                    return _FakeResp(200, {"data": []})
                return _FakeResp(200, {"data": [
                    {"providerID": "火山AI网关", "id": "deepseek-v4-flash"},
                ]})
            return await super().get(url, timeout=timeout)

    client = _CatalogClient(_turn_lines())
    spec = SessionSpec(cwd="/tmp", model="火山AI网关/deepseek-v4-flash")
    session = _session(client, spec=spec)
    events = _send_and_drain(session)
    assert client.model_polls >= 2
    # The prompt was still submitted and the turn completed normally.
    assert any(u.endswith("/prompt") for u, _ in client.posts)
    assert events[-1].payload["reason"] == "success"


def test_warmup_polls_only_on_first_turn() -> None:
    client = _FakeClient(_turn_lines())
    spec = SessionSpec(cwd="/tmp", model="火山AI网关/deepseek-v4-flash")
    session = _session(client, spec=spec)

    async def run() -> None:
        await session.send("first")
        async for _ in session.events():
            pass
        polls_after_first = len(client.gets)
        await session.send("second")
        async for _ in session.events():
            pass
        assert len(client.gets) == polls_after_first  # no re-poll

    asyncio.run(run())


def test_model_ref_multi_segment_id() -> None:
    """Provider is the segment before the FIRST slash; the rest —
    including further slashes — is the model id.
    """
    s = OpenCodeSession(SessionSpec(cwd="/tmp", model="opencode/a/b"))
    assert s._model_ref() == {"providerID": "opencode", "id": "a/b"}


# ---------------------------------------------------------------------------
# Usage across multiple steps
# ---------------------------------------------------------------------------


def test_usage_sums_across_steps() -> None:
    lines = [
        _frame("session.next.prompt.admitted", sessionID=OC_SID),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="tool-calls",
            tokens={"input": 100, "output": 10, "reasoning": 0,
                    "cache": {"read": 0, "write": 5}},
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={"input": 50, "output": 3, "reasoning": 2,
                    "cache": {"read": 7, "write": 0}},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    assert events[-1].payload["usage"] == {
        "input": 150,
        "output": 13,
        "reasoning": 2,
        "cache_read": 7,
        "cache_write": 5,
    }


# ---------------------------------------------------------------------------
# Approval fallback + full DENY loop
# ---------------------------------------------------------------------------


def test_permission_asked_without_tool_call_uses_resources_fallback() -> None:
    """No tool.called frame was seen for this callID — arguments degrade
    to the bus frame's ``resources`` list instead of an empty dict.
    """
    lines = [
        _frame(
            "permission.v2.asked",
            sessionID=OC_SID,
            id="per_req2",
            action="edit",
            resources=["src/a.py"],
            source={"type": "tool", "messageID": "msg_1", "callID": "call_9"},
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    approvals = [e for e in events if e.kind is EventKind.APPROVAL_REQUEST]
    assert len(approvals) == 1
    assert approvals[0].payload["arguments"] == {"resources": ["src/a.py"]}


def test_deny_loop_tool_failure_surfaces_and_turn_continues() -> None:
    """approve(reject) → opencode fails the tool (tool.failed) →
    TOOL_RESULT ok=False → the turn still completes (the agent sees the
    denial and adapts). This is the wire shape after a core DENY.
    """
    lines = [
        _frame(
            "session.next.tool.called",
            sessionID=OC_SID,
            callID="call_1",
            tool="bash",
            input={"command": "rm -rf /"},
        ),
        _frame(
            "permission.v2.asked",
            sessionID=OC_SID,
            id="per_deny1",
            action="bash",
            resources=["rm -rf /"],
            source={"type": "tool", "messageID": "msg_1", "callID": "call_1"},
        ),
        _frame(
            "session.next.tool.failed",
            sessionID=OC_SID,
            callID="call_1",
            content=[{"type": "text", "text": "Permission denied by operator"}],
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)

    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(results) == 1
    assert results[0].payload["ok"] is False
    assert "denied" in results[0].payload["output"]
    # The turn completed despite the denied tool.
    assert events[-1].payload["reason"] == "success"

    # The core's DENY decision posts "reject".
    asyncio.run(session.approve("per_deny1", ApprovalDecision.DENY))
    assert client.posts[-1] == (
        f"/api/session/{OC_SID}/permission/per_deny1/reply",
        {"reply": "reject"},
    )


def test_deny_abandonment_synthesizes_honest_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a rejection opencode fails the tool and ABANDONS the
    turn — no step.ended, no further events, interrupt() cannot unwind
    it (verified live). The backend must synthesize the terminal after
    the grace window instead of burning the core's inactivity budget,
    and the run must fail with the denial as the stated cause.
    """
    monkeypatch.setattr(session_mod, "_DENY_GRACE_SECONDS", 0.2)
    lines = [
        _frame("session.next.prompt.admitted", sessionID=OC_SID),
        _frame(
            "session.next.tool.called",
            sessionID=OC_SID,
            callID="call_1",
            tool="bash",
            input={"command": "x"},
        ),
        _frame(
            "permission.v2.asked",
            sessionID=OC_SID,
            id="per_d1",
            action="bash",
            resources=["x"],
            source={"type": "tool", "messageID": "msg_1", "callID": "call_1"},
        ),
        _frame(
            "permission.v2.replied",
            sessionID=OC_SID,
            requestID="per_d1",
            reply="reject",
        ),
        _frame(
            "session.next.tool.failed",
            sessionID=OC_SID,
            callID="call_1",
            error={"type": "unknown", "message": "Tool execution interrupted"},
        ),
        # ... then the bus stays open but silent — exactly the observed
        # abandonment shape. The grace timer must fire.
    ]

    class _HangingStreamResp(_FakeStreamResp):
        async def aiter_lines(self):
            for line in self._lines:
                yield line
            await asyncio.sleep(3600)

    class _HangingClient(_FakeClient):
        def stream(self, method, url, *, headers=None, timeout=None):
            self.stream_calls.append((method, url))
            return _HangingStreamResp(
                self._stream_lines, 200, "text/event-stream"
            )

    client = _HangingClient(lines)
    session = _session(client)
    events = _send_and_drain(session)

    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "opencode_approval_denied"
    assert "'bash'" in errors[0].payload["message"]
    turns = [e for e in events if e.kind is EventKind.TURN_COMPLETE]
    assert turns[-1].payload["finish"] == "denied"
    assert events[-1].kind is EventKind.SESSION_COMPLETE
    assert events[-1].payload["reason"] == "error"
    assert "approval denied" in events[-1].payload["message"]


def test_deny_grace_cleared_by_model_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward compatibility: if opencode recovers and the model keeps
    working after a rejection (deltas / new steps), the synthesized
    termination must NOT fire — the turn completes normally.
    """
    monkeypatch.setattr(session_mod, "_DENY_GRACE_SECONDS", 0.2)
    lines = [
        _frame("session.next.prompt.admitted", sessionID=OC_SID),
        _frame(
            "permission.v2.asked",
            sessionID=OC_SID,
            id="per_d2",
            action="edit",
            resources=["a.py"],
            source={"type": "tool", "messageID": "msg_1", "callID": "call_1"},
        ),
        _frame(
            "permission.v2.replied",
            sessionID=OC_SID,
            requestID="per_d2",
            reply="reject",
        ),
        _frame(
            "session.next.tool.failed",
            sessionID=OC_SID,
            callID="call_1",
            error={"type": "unknown", "message": "Tool execution interrupted"},
        ),
        # The model adapts within the grace window:
        _frame(
            "session.next.text.delta",
            sessionID=OC_SID,
            textID="text-0",
            delta="Understood — using an alternative approach.",
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    assert not [
        e
        for e in events
        if e.kind is EventKind.ERROR
        and e.payload.get("code") == "opencode_approval_denied"
    ]
    assert events[-1].payload["reason"] == "success"


# ---------------------------------------------------------------------------
# Tool result content fallbacks (read/glob/grep carry their payload in
# structured.content with an EMPTY content list; tool.failed carries its
# reason under error.message).
# ---------------------------------------------------------------------------


def test_read_tool_result_uses_structured_content() -> None:
    lines = [
        _frame("session.next.prompt.admitted", sessionID=OC_SID),
        _frame(
            "session.next.tool.success",
            sessionID=OC_SID,
            callID="call_r1",
            structured={
                "uri": "file:///tmp/x",
                "name": "x.txt",
                "content": "MARKER-CONTENT-12345\n",
                "encoding": "utf8",
            },
            content=[],
            outputPaths=[],
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(results) == 1
    assert results[0].payload["output"] == "MARKER-CONTENT-12345\n"


def test_read_tool_result_falls_back_to_structured_dump() -> None:
    """structured without a content field (e.g. glob/grep shapes) still
    surfaces something diagnosable — never None.
    """
    lines = [
        _frame("session.next.prompt.admitted", sessionID=OC_SID),
        _frame(
            "session.next.tool.success",
            sessionID=OC_SID,
            callID="call_g1",
            structured={"operation": "glob", "matches": ["a.py", "b.py"]},
            content=[],
            outputPaths=[],
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert results[0].payload["output"] is not None
    assert "glob" in results[0].payload["output"]


def test_tool_failed_uses_error_message() -> None:
    lines = [
        _frame("session.next.prompt.admitted", sessionID=OC_SID),
        _frame(
            "session.next.tool.failed",
            sessionID=OC_SID,
            callID="call_f1",
            error={"type": "unknown", "message": "Tool execution interrupted"},
        ),
        _frame(
            "session.next.step.ended",
            sessionID=OC_SID,
            finish="stop",
            tokens={},
        ),
    ]
    client = _FakeClient(lines)
    session = _session(client)
    events = _send_and_drain(session)
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert results[0].payload["ok"] is False
    assert results[0].payload["output"] == "Tool execution interrupted"


# ---------------------------------------------------------------------------
# Lifecycle edges
# ---------------------------------------------------------------------------


def test_close_during_blocked_turn_terminates_drain() -> None:
    """Operator stop (close) while the turn is blocked must terminate
    the events() drain with the terminal envelope — never hang.
    """
    gate = asyncio.Event()

    class _GatedClient(_FakeClient):
        def stream(self, method, url, *, headers=None, timeout=None):
            self.stream_calls.append((method, url))
            return _GatedStreamResp(self._stream_lines)

    class _GatedStreamResp(_FakeStreamResp):
        async def aiter_lines(self):
            await gate.wait()
            for line in self._lines:
                yield line

    client = _GatedClient(_turn_lines())
    session = _session(client)

    async def run() -> list[Any]:
        await session.send("hi")
        drain = asyncio.ensure_future(_collect(session))
        await asyncio.sleep(0.1)  # let the pump block on the gate
        assert not drain.done()
        await session.close()
        return await asyncio.wait_for(drain, timeout=5.0)

    async def _collect(s: OpenCodeSession) -> list[Any]:
        out: list[Any] = []
        async for ev in s.events():
            out.append(ev)
        return out

    events = asyncio.run(run())
    assert events, "drain must still see events"
    assert events[-1].kind is EventKind.SESSION_COMPLETE


def test_probe_resume_survives_server_spawn_failure() -> None:
    """Bootstrap failure during the probe is UNDETECTABLE — the caller
    may still attempt send() and surface the real error there.
    """
    client = _FakeClient()
    spec = SessionSpec(cwd="/tmp", resume_session_id="ses_target123")
    session = _session(client, spec=spec)

    async def _boom() -> Any:
        raise _TransportError("opencode_port_discovery", "no banner")

    session._ensure_server = _boom  # type: ignore[method-assign]
    assert asyncio.run(session.probe_resume()) is ResumeStatus.UNDETECTABLE


def test_ensure_server_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second _ensure_server must reuse the running serve — one spawn
    per session, not one per HTTP call.
    """
    spawn_count = 0

    class _CountingProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(
                [b"opencode server listening on http://127.0.0.1:45551\n"]
            )

    async def fake_exec(*args: Any, **kwargs: Any) -> _CountingProc:
        nonlocal spawn_count
        spawn_count += 1
        return _CountingProc()

    monkeypatch.setattr(
        session_mod.asyncio, "create_subprocess_exec", fake_exec
    )
    session = OpenCodeSession(
        SessionSpec(cwd="/tmp"), client_factory=lambda port: _FakeClient()
    )

    async def run() -> tuple[Any, Any]:
        c1 = await session._ensure_server()
        c2 = await session._ensure_server()
        return c1, c2

    c1, c2 = asyncio.run(run())
    assert c1 is c2
    assert spawn_count == 1


def test_ensure_server_reaps_stale_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previous failed attempt leaves a half-started serve behind —
    the next _ensure_server must reap it before spawning a fresh one.
    """
    procs: list[_FakeProc] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        procs.append(
            _FakeProc(
                [b"opencode server listening on http://127.0.0.1:45551\n"]
            )
        )
        return procs[-1]

    monkeypatch.setattr(
        session_mod.asyncio, "create_subprocess_exec", fake_exec
    )
    session = OpenCodeSession(
        SessionSpec(cwd="/tmp"), client_factory=lambda port: _FakeClient()
    )
    # Simulate a half-started leftover: proc set, port discovery failed.
    stale = _FakeProc([])
    session._proc = stale

    asyncio.run(session._ensure_server())

    assert stale.terminated, "stale serve must be reaped"
    assert len(procs) == 1, "exactly one fresh serve spawned"
    assert session._proc is procs[0]


# ---------------------------------------------------------------------------
# Source-level regression guard: the retry/close race
# ---------------------------------------------------------------------------


def test_no_tracker_close_immediately_before_retry_schedule() -> None:
    """Source scan: a ``failed`` tracker sync must never directly
    precede ``_schedule_retry`` — that ordering closes the issue on the
    tracker while the retry is pending, and GitCode cannot reopen it
    (the persisted retry plan was silently dropped this way).

    Line-index based (regex `[^)]*` cannot span multi-line calls).
    """
    from pathlib import Path

    src_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "orchestratord"
        / "orchestrator.py"
    )
    lines = src_path.read_text().splitlines()
    sync_idx = [
        i for i, line in enumerate(lines)
        if "_sync_tracker_issue_state(" in line
    ]
    schedule_idx = [
        i for i, line in enumerate(lines)
        if "self._schedule_retry(" in line
    ]
    offenders = [
        (s + 1, sched + 1)
        for s in sync_idx
        for sched in schedule_idx
        if 0 < sched - s <= 6
    ]
    assert not offenders, (
        f"tracker sync directly before _schedule_retry at "
        f"{src_path.name}:{offenders} — the close/retry race is "
        "reintroduced"
    )
