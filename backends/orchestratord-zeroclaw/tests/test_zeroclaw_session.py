"""ZeroClaw session tests — real ACP wire translation against a scripted fake.

The tests spawn ``_fake_zeroclaw_cli.py`` (a Python reproduction of the
frames a real ZeroClaw 0.8.4 binary sends over its ACP JSON-RPC 2.0
stdio transport, mirroring the Go suite's ``fakeZeroclawACPScript``) via
``sys.executable`` and drive it through ``ZeroclawSession``'s NDJSON
client. Covered: the happy path (initialize → session/new →
session/prompt → agent_message_chunk deltas → {stopReason, usage}),
agentAlias lifting, session/resume (never session/load) including
replay gating and -32000 SESSION_NOT_FOUND, tool_call translation, the
session/request_permission auto-answer policy (with ZeroClaw's legacy
choice bridge failing closed), error propagation, and the total timeout.
No test touches a real installed CLI.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest
from orchestratord_zeroclaw.backend import ZeroclawBackend
from orchestratord_zeroclaw.session import ZeroclawSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventEnvelope, EventKind

_FAKE_CLI = Path(__file__).with_name("_fake_zeroclaw_cli.py")


def _make_spec(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    extra: dict | None = None,
    resume: str | None = None,
    system_prompt: str | None = None,
    total_timeout_s: float | None = None,
) -> SessionSpec:
    return SessionSpec(
        cwd=str(tmp_path),
        env=env or {},
        resume_session_id=resume,
        system_prompt=system_prompt,
        total_timeout_s=total_timeout_s,
        extra={"command": [sys.executable, str(_FAKE_CLI)], **(extra or {})},
    )


async def _run(session: ZeroclawSession, prompt: str = "test prompt") -> list[EventEnvelope]:
    await session.send(prompt)
    return [ev async for ev in session.events()]


def _frames(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _request_frames(path: Path) -> list[dict]:
    return [f for f in _frames(path) if f.get("method")]


# -- capability consistency ----------------------------------------------------


def test_session_capabilities_match_backend() -> None:
    """The session-level bit set must equal backend.capabilities()."""
    backend = ZeroclawBackend()
    spec = SessionSpec(cwd="/tmp")
    session = ZeroclawSession(spec)
    assert session.capabilities == backend.capabilities()
    assert session.capabilities.streaming_deltas is True


def test_backend_capabilities_match_descriptor() -> None:
    backend = ZeroclawBackend()
    descriptor_bits = {"streaming_deltas", "parallel_sessions"}
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits


# -- happy path ------------------------------------------------------------------


def test_happy_path_streams_deltas_and_completes(tmp_path: Path) -> None:
    req_file = tmp_path / "requests.txt"
    session = ZeroclawSession(
        _make_spec(tmp_path, env={"ZEROCLAW_REQUESTS_FILE": str(req_file)})
    )
    events = asyncio.run(_run(session))

    kinds = [ev.kind for ev in events]
    assert kinds[-2:] == [EventKind.TURN_COMPLETE, EventKind.SESSION_COMPLETE]
    assert events[-2].payload == {"reason": "success"}
    # Streaming: one TEXT_DELTA per agent_message_chunk frame, not one
    # buffered TEXT blob.
    deltas = [ev for ev in events if ev.kind is EventKind.TEXT_DELTA]
    assert [ev.payload["delta"] for ev in deltas] == ["CURRENT ", "ANSWER"]
    assert "".join(ev.payload["text"] for ev in deltas) == "CURRENT ANSWER"
    # The turn's answer must not be collapsed into a single TEXT event.
    assert all(ev.kind is not EventKind.TEXT for ev in events)

    complete = events[-1].payload
    assert complete["reason"] == "success"
    assert complete["session_id"] == "ses_zeroclaw_new"
    assert session.session_id == "ses_zeroclaw_new"

    frames = _request_frames(req_file)
    assert [f["method"] for f in frames] == [
        "initialize",
        "session/new",
        "session/prompt",
    ]
    new_params = frames[1]["params"]
    assert new_params["cwd"] == str(tmp_path)
    assert new_params["mcpServers"] == []
    assert "agentAlias" not in new_params
    prompt_frame = frames[2]
    assert prompt_frame["params"]["prompt"] == [
        {"type": "text", "text": "test prompt"}
    ]


def test_system_prompt_is_folded_into_the_prompt_text(tmp_path: Path) -> None:
    req_file = tmp_path / "requests.txt"
    session = ZeroclawSession(
        _make_spec(
            tmp_path,
            system_prompt="be terse",
            env={"ZEROCLAW_REQUESTS_FILE": str(req_file)},
        )
    )
    events = asyncio.run(_run(session))
    assert events[-1].payload["reason"] == "success"
    frame = next(
        f for f in _request_frames(req_file) if f["method"] == "session/prompt"
    )
    assert frame["params"]["prompt"] == [
        {"type": "text", "text": "be terse\n\n---\n\ntest prompt"}
    ]


def test_usage_is_published_on_session_complete(tmp_path: Path) -> None:
    session = ZeroclawSession(_make_spec(tmp_path))
    events = asyncio.run(_run(session))
    usage = events[-1].payload["usage"]
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 20


def test_configured_model_is_not_put_on_argv(tmp_path: Path) -> None:
    """``zeroclaw acp`` has no model flag and no session/set_model; a
    configured model must not fail the run (Go: TestZeroclawDoesNotAttemptModelSelection)."""
    session = ZeroclawSession(
        _make_spec(tmp_path, extra={"custom_args": []})
    )
    session._spec.model = "zeroclaw-large"
    events = asyncio.run(_run(session))
    assert events[-1].payload["reason"] == "success"


# -- agentAlias lifting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("custom_args", "expected_argv_tail"),
    [
        pytest.param(["--agent", "myagent"], [], id="separate-value"),
        pytest.param(["--agent=myagent"], [], id="inline-value"),
        pytest.param(["--agent-alias", "myagent"], [], id="long-spelling"),
        pytest.param(
            ["--log-level", "debug", "--agent", "myagent", "--verbose"],
            ["--log-level", "debug", "--verbose"],
            id="consumed-out-of-the-middle",
        ),
    ],
)
def test_agent_alias_travels_as_session_new_param_not_argv(
    tmp_path: Path,
    custom_args: list[str],
    expected_argv_tail: list[str],
) -> None:
    req_file = tmp_path / "requests.txt"
    argv_file = tmp_path / "argv.txt"
    session = ZeroclawSession(
        _make_spec(
            tmp_path,
            env={
                "ZEROCLAW_REQUESTS_FILE": str(req_file),
                "ZEROCLAW_ARGV_FILE": str(argv_file),
            },
            extra={"custom_args": custom_args},
        )
    )
    events = asyncio.run(_run(session))
    assert events[-1].payload["reason"] == "success"

    frame = next(
        f for f in _request_frames(req_file) if f["method"] == "session/new"
    )
    assert frame["params"]["agentAlias"] == "myagent"
    assert json.loads(argv_file.read_text()) == ["acp", *expected_argv_tail]


def test_whitespace_alias_counts_as_unset(tmp_path: Path) -> None:
    req_file = tmp_path / "requests.txt"
    session = ZeroclawSession(
        _make_spec(
            tmp_path,
            env={"ZEROCLAW_REQUESTS_FILE": str(req_file)},
            extra={"custom_args": ["--agent", "   "]},
        )
    )
    events = asyncio.run(_run(session))
    assert events[-1].payload["reason"] == "success"
    frame = next(
        f for f in _request_frames(req_file) if f["method"] == "session/new"
    )
    assert "agentAlias" not in frame["params"]


# -- resume -----------------------------------------------------------------------


def test_resume_uses_session_resume_never_session_load(tmp_path: Path) -> None:
    req_file = tmp_path / "requests.txt"
    session = ZeroclawSession(
        _make_spec(
            tmp_path,
            resume="ses_existing",
            env={"ZEROCLAW_REQUESTS_FILE": str(req_file)},
        )
    )
    events = asyncio.run(_run(session))

    assert events[-1].payload["reason"] == "success"
    assert events[-1].payload["session_id"] == "ses_existing"

    methods = [f["method"] for f in _request_frames(req_file)]
    assert "session/new" not in methods
    assert "session/load" not in methods
    assert "session/resume" in methods
    resume_frame = next(
        f
        for f in _request_frames(req_file)
        if f["method"] == "session/resume"
    )
    # session/resume must send only the session id ZeroClaw reads.
    assert resume_frame["params"] == {"sessionId": "ses_existing"}


def test_resume_drops_replayed_history(tmp_path: Path) -> None:
    """A historical agent_message_chunk pushed before the resume answer
    belongs to a prior turn — the gate must swallow it (Go:
    TestZeroclawResumeDropsReplayedHistory)."""
    session = ZeroclawSession(
        _make_spec(tmp_path, resume="ses_existing", env={"ZEROCLAW_STALE_REPLAY": "1"})
    )
    events = asyncio.run(_run(session))
    deltas = [ev.payload["text"] for ev in events if ev.kind is EventKind.TEXT_DELTA]
    assert deltas == ["CURRENT ", "ANSWER"]
    assert events[-1].payload["reason"] == "success"


def test_resume_not_found_reports_rejection(tmp_path: Path) -> None:
    session = ZeroclawSession(
        _make_spec(tmp_path, resume="ses_gone", env={"ZEROCLAW_SESSION_NOT_FOUND": "1"})
    )
    events = asyncio.run(_run(session))
    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "zeroclaw_resume_rejected"
    assert "session/resume failed" in errors[0].payload["message"]
    assert events[-2].payload["reason"] == "error"
    assert events[-1].payload["reason"] == "error"
    assert "session_id" not in events[-1].payload


def test_resume_capability_unavailable_reports_rejection(tmp_path: Path) -> None:
    """Persistence unavailable → initialize omits sessionCapabilities.resume;
    report positive rejection evidence without touching the wire further."""
    req_file = tmp_path / "requests.txt"
    session = ZeroclawSession(
        _make_spec(
            tmp_path,
            resume="ses_existing",
            env={
                "ZEROCLAW_NO_RESUME_CAP": "1",
                "ZEROCLAW_REQUESTS_FILE": str(req_file),
            },
        )
    )
    events = asyncio.run(_run(session))
    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "zeroclaw_resume_unavailable"
    assert "sessionCapabilities.resume" in errors[0].payload["message"]
    methods = [f["method"] for f in _request_frames(req_file)]
    assert methods == ["initialize"]


# -- tool calls ----------------------------------------------------------------------


def test_tool_call_and_result_are_translated(tmp_path: Path) -> None:
    session = ZeroclawSession(
        _make_spec(tmp_path, env={"ZEROCLAW_TOOL_CALL": "1"})
    )
    events = asyncio.run(_run(session))
    calls = [ev for ev in events if ev.kind is EventKind.TOOL_CALL]
    results = [ev for ev in events if ev.kind is EventKind.TOOL_RESULT]
    assert len(calls) == 1 and len(results) == 1
    assert calls[0].payload == {
        "call_id": "call_1",
        "name": "terminal",
        "arguments": {"command": "echo hi"},
    }
    assert results[0].payload["call_id"] == "call_1"
    assert results[0].payload["ok"] is True
    assert results[0].payload["output"] == "hi\n"


# -- permission policy -----------------------------------------------------------------


def test_permission_request_is_auto_answered_allow_once(tmp_path: Path) -> None:
    req_file = tmp_path / "requests.txt"
    session = ZeroclawSession(
        _make_spec(
            tmp_path,
            env={"ZEROCLAW_PERMISSION": "1", "ZEROCLAW_REQUESTS_FILE": str(req_file)},
        )
    )
    events = asyncio.run(_run(session))
    assert events[-1].payload["reason"] == "success"
    # The client's answer frame carries no method; find the selected outcome.
    answers = [
        f
        for f in _frames(req_file)
        if "method" not in f and isinstance(f.get("result"), dict)
        and "outcome" in f["result"]
    ]
    assert answers, "expected a request_permission answer on the wire"
    outcome = answers[0]["result"]["outcome"]
    assert outcome == {"outcome": "selected", "optionId": "allow-once"}


def test_legacy_choice_bridge_fails_closed(tmp_path: Path) -> None:
    """ZeroClaw's structured question bridge labels every answer choice-N;
    the client must answer -32603 and the prompt fails through (Go:
    TestZeroclawLegacyChoiceReturnsProtocolErrorThroughClient)."""
    req_file = tmp_path / "requests.txt"
    session = ZeroclawSession(
        _make_spec(
            tmp_path,
            env={
                "ZEROCLAW_LEGACY_CHOICE": "1",
                "ZEROCLAW_REQUESTS_FILE": str(req_file),
            },
        )
    )
    events = asyncio.run(_run(session))
    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "zeroclaw_prompt"
    assert "ACP request_permission failed" in errors[0].payload["message"]
    answers = [f for f in _frames(req_file) if "method" not in f and "error" in f]
    assert answers and answers[0]["error"]["code"] == -32603
    assert events[-1].payload["reason"] == "error"


# -- error propagation -----------------------------------------------------------------


def test_session_new_alias_error_is_actionable(tmp_path: Path) -> None:
    session = ZeroclawSession(
        _make_spec(tmp_path, env={"ZEROCLAW_REQUIRE_AGENT_ALIAS": "1"})
    )
    events = asyncio.run(_run(session))
    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "zeroclaw_session_new"
    for wanted in ("--agent", "default_agent"):
        assert wanted in errors[0].payload["message"]
    assert events[-2].payload["reason"] == "error"
    assert events[-1].payload["reason"] == "error"


def test_process_dying_mid_handshake_surfaces_as_error(tmp_path: Path) -> None:
    session = ZeroclawSession(
        _make_spec(tmp_path, total_timeout_s=10)
    )
    session._spec.extra["command"] = [
        sys.executable,
        "-c",
        "raise SystemExit(3)",
        "acp",
    ]
    events = asyncio.run(_run(session))
    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] in {
        "zeroclaw_exit",
        "zeroclaw_error",
        "zeroclaw_spawn_error",
    }
    assert events[-2].payload["reason"] == "error"
    assert events[-1].payload["reason"] == "error"


def test_total_timeout_kills_the_turn(tmp_path: Path) -> None:
    session = ZeroclawSession(
        _make_spec(tmp_path, env={"ZEROCLAW_HANG": "1"}, total_timeout_s=0.5)
    )
    events = asyncio.run(_run(session))
    errors = [ev for ev in events if ev.kind is EventKind.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "zeroclaw_timeout"
    assert "0.5s" in errors[0].payload["message"]
    assert events[-2].payload["reason"] == "timeout"
    assert events[-1].payload["reason"] == "timeout"


def test_send_after_close_raises(tmp_path: Path) -> None:
    session = ZeroclawSession(_make_spec(tmp_path))
    asyncio.run(session.close())
    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(_run(session))
