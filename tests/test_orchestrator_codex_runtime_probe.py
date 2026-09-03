"""Tests for Scheme A: codex runtime probe.

Verifies that:

1. ``_detect_runtime`` returns ``"as"`` when ``codex app-server --help``
   exits 0, and ``"cli"`` when the subprocess is missing or fails.

2. :class:`CodexBackend` reflects the probed runtime:

   * ``"as"`` → capability bits include ``streaming_deltas``,
     ``interrupt``, ``approval_hooks`` (4/8).
   * ``"cli"`` → only ``resumable`` and ``parallel_sessions`` (2/8).

3. :meth:`CodexBackend.create_session` picks the matching session class.

4. :func:`_detect_runtime` falls back to ``cli`` if the asyncio probe
   itself raises (defensive default).

5. ``capabilities()`` is deterministic across repeated calls for the
   same runtime (per Scheme A §1.6 contract test).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from unittest.mock import patch

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventKind

from orchestratord_codex import CodexAppServerSession, CodexSession
from orchestratord_codex.backend import (
    CodexBackend,
    _detect_runtime,
    _probe_app_server,
)


# ---------------------------------------------------------------------------
# Probe unit tests
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` used by the probe."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_probe_app_server_true_when_help_succeeds() -> None:
    """A ``codex app-server --help`` that exits 0 ⇒ probe returns True."""

    captured: list[list[str]] = []

    async def _fake_exec(*args, **kwargs):
        captured.append(list(args))
        return _FakeProc(0)

    with patch(
        "orchestratord_codex.backend.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        with patch(
            "orchestratord_codex.backend.shutil.which",
            return_value="/usr/bin/codex",
        ):
            ok = await _probe_app_server()

    assert ok is True
    assert captured == [["/usr/bin/codex", "app-server", "--help"]]


@pytest.mark.asyncio
async def test_probe_app_server_false_when_help_fails() -> None:
    """A ``codex app-server --help`` that exits non-zero ⇒ probe returns False."""

    async def _fake_exec(*args, **kwargs):
        return _FakeProc(64)

    with patch(
        "orchestratord_codex.backend.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        with patch(
            "orchestratord_codex.backend.shutil.which",
            return_value="/usr/bin/codex",
        ):
            ok = await _probe_app_server()

    assert ok is False


@pytest.mark.asyncio
async def test_probe_app_server_false_when_binary_missing() -> None:
    """``shutil.which`` returning ``None`` skips the subprocess spawn
    entirely — no ``FileNotFoundError`` raised to callers.
    """
    with patch(
        "orchestratord_codex.backend.shutil.which", return_value=None
    ):
        ok = await _probe_app_server()
    assert ok is False


def test_detect_runtime_returns_cli_on_probe_exception() -> None:
    """If the asyncio loop explodes during the probe, ``_detect_runtime``
    must default to ``cli`` (safe fallback; never raises to caller).
    """

    def _explode() -> bool:
        raise RuntimeError("loop unavailable")

    with patch(
        "orchestratord_codex.backend.asyncio.run",
        side_effect=_explode,
    ):
        runtime = _detect_runtime()
    assert runtime == "cli"


# ---------------------------------------------------------------------------
# Backend construction tests
# ---------------------------------------------------------------------------


def _spec() -> SessionSpec:
    return SessionSpec(cwd="/tmp")


def test_backend_cli_runtime_exposes_2_of_8() -> None:
    """When the probe resolves to ``cli``, the backend advertises the
    pre-Scheme-A capability set (2/8): ``resumable`` + ``parallel_sessions``.
    """
    backend = CodexBackend()
    backend._runtime = "cli"  # noqa: SLF001 — direct override in test

    caps = backend.capabilities()
    assert isinstance(caps, BackendCapabilities)
    assert caps.streaming_deltas is False
    assert caps.resumable is True
    assert caps.interrupt is False
    assert caps.approval_hooks is False
    assert caps.parallel_sessions is True
    assert caps.cost_reporting is False
    assert caps.tool_filtering is False
    assert caps.takeover is False


def test_backend_as_runtime_exposes_4_of_8() -> None:
    """When the probe resolves to ``as``, the backend advertises the
    full SdkProcess capability set (4/8): ``streaming_deltas +
    interrupt + approval_hooks + parallel_sessions``.
    """
    backend = CodexBackend()
    backend._runtime = "as"  # noqa: SLF001

    caps = backend.capabilities()
    assert caps.streaming_deltas is True
    assert caps.interrupt is True
    assert caps.approval_hooks is True
    assert caps.parallel_sessions is True
    # Bits we have not earned:
    assert caps.resumable is False
    assert caps.cost_reporting is False
    assert caps.tool_filtering is False
    assert caps.takeover is False


def test_create_session_picks_cli_session_when_runtime_cli() -> None:
    """The Cli branch yields a :class:`CodexSession` instance."""
    backend = CodexBackend()
    backend._runtime = "cli"  # noqa: SLF001
    session = backend.create_session(_spec())
    assert isinstance(session, CodexSession)
    assert not isinstance(session, CodexAppServerSession)


def test_cli_session_translates_codex_jsonl_to_typed_events() -> None:
    session = CodexSession(_spec())

    assert session._translate_wire_event(  # noqa: SLF001
        {"type": "thread.started", "thread_id": "thread-7"}, timestamp=10.0
    ) == []
    session._translate_wire_event({"type": "turn.started"}, timestamp=11.0)  # noqa: SLF001
    started = session._translate_wire_event(  # noqa: SLF001
        {
            "type": "item.started",
            "item": {
                "id": "command-1",
                "type": "command_execution",
                "command": "python -m pytest -q",
                "status": "in_progress",
            },
        },
        timestamp=12.0,
    )
    completed = session._translate_wire_event(  # noqa: SLF001
        {
            "type": "item.completed",
            "item": {
                "id": "command-1",
                "type": "command_execution",
                "command": "python -m pytest -q",
                "aggregated_output": "3 passed",
                "exit_code": 0,
                "status": "completed",
            },
        },
        timestamp=15.5,
    )
    finished = session._translate_wire_event(  # noqa: SLF001
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 120, "output_tokens": 14},
        },
        timestamp=16.0,
    )

    events = started + completed + finished
    assert session.session_id == "thread-7"
    assert [event.kind for event in events] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TURN_COMPLETE,
    ]
    assert events[0].timestamp == 12.0
    assert events[0].payload["arguments"] == {
        "command": "python -m pytest -q"
    }
    assert events[1].timestamp == 15.5
    assert events[1].payload["output"] == "3 passed"
    assert events[2].payload["usage"]["input_tokens"] == 120


@pytest.mark.asyncio
async def test_cli_session_streams_events_while_process_is_running() -> None:
    release = asyncio.Event()
    spawned: dict[str, object] = {}
    lines = [
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "msg-1", "type": "agent_message", "text": "Working."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 9, "output_tokens": 2},
        },
    ]

    class FakeStdout:
        def __init__(self) -> None:
            self.index = 0

        async def readline(self) -> bytes:
            if self.index == 0:
                await release.wait()
            if self.index >= len(lines):
                return b""
            value = (json.dumps(lines[self.index]) + "\n").encode()
            self.index += 1
            return value

    class FakeStderr:
        async def read(self) -> bytes:
            return b""

    class FakeStdin:
        def write(self, value: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

        def terminate(self) -> None:
            self.returncode = -15

    async def fake_exec(*args, **kwargs):
        spawned.update(kwargs)
        return FakeProcess()

    session = CodexSession(SessionSpec(cwd="/tmp", env={"ORCH_TEST_ENV": "1"}))
    with patch(
        "orchestratord_codex.session.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        await session.send("inspect")
        assert session._run_task is not None  # noqa: SLF001
        assert session._run_task.done() is False  # noqa: SLF001
        release.set()
        events = [event async for event in session.events()]

    assert [event.kind for event in events] == [
        EventKind.TEXT,
        EventKind.TURN_COMPLETE,
        EventKind.SESSION_COMPLETE,
    ]
    assert events[0].payload["text"] == "Working."
    assert events[-1].payload["usage"] == {"input_tokens": 9, "output_tokens": 2}
    assert spawned["env"]["ORCH_TEST_ENV"] == "1"
    assert "PATH" in spawned["env"]


def test_create_session_picks_app_server_session_when_runtime_as() -> None:
    """The AppServer branch yields a :class:`CodexAppServerSession`."""
    backend = CodexBackend()
    backend._runtime = "as"  # noqa: SLF001
    session = backend.create_session(_spec())
    assert isinstance(session, CodexAppServerSession)


def test_cli_session_reads_new_prompt_from_stdin() -> None:
    """Prompts beginning with dashes must not be parsed as CLI options."""
    session = CodexSession(SessionSpec(cwd="/tmp", model="gpt-5.5"))

    argv, stdin_payload = session._build_argv("---\noperator follow-up")  # noqa: SLF001

    assert argv == ["codex", "exec", "--json", "-m", "gpt-5.5", "-"]
    assert stdin_payload == b"---\noperator follow-up"


def test_cli_session_applies_workflow_reasoning_override_before_exec() -> None:
    """A per-workflow override must bypass an incompatible global config."""
    session = CodexSession(
        SessionSpec(
            cwd="/tmp",
            model="gpt-5.5",
            env={"ORCHESTRATORD_CODEX_REASONING_EFFORT": "xhigh"},
        )
    )

    argv, stdin_payload = session._build_argv("inspect")  # noqa: SLF001

    assert argv == [
        "codex",
        "-c",
        'model_reasoning_effort="xhigh"',
        "exec",
        "--json",
        "-m",
        "gpt-5.5",
        "-",
    ]
    assert stdin_payload == b"inspect"


@pytest.mark.asyncio
async def test_app_server_session_inherits_process_environment() -> None:
    """App-server subprocesses need PATH plus explicit workflow overrides."""
    captured: dict[str, object] = {}

    class FakeWorker:
        def __init__(self, *, worker_cmd, cwd, env):
            captured.update(worker_cmd=worker_cmd, cwd=cwd, env=env)

        async def start(self) -> None:
            return None

    session = CodexAppServerSession(
        SessionSpec(cwd="/tmp", env={"ORCH_TEST_ENV": "1"})
    )
    with patch(
        "orchestratord_codex.app_server_session.WorkerManager",
        FakeWorker,
    ):
        await session._ensure_worker()  # noqa: SLF001

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["ORCH_TEST_ENV"] == "1"
    assert "PATH" in child_env


def test_runtime_property_reflects_probe() -> None:
    """The ``runtime`` property exposes the probed value for diagnostics."""
    backend = CodexBackend()
    backend._runtime = "as"  # noqa: SLF001
    assert backend.runtime == "as"
    backend._runtime = "cli"  # noqa: SLF001
    assert backend.runtime == "cli"


def test_capabilities_is_deterministic() -> None:
    """Per Scheme A §1.6: capabilities() must be stable across repeated
    calls for the same probed runtime — never oscillate between the
    Cli and As sets within one backend instance.
    """
    backend = CodexBackend()
    backend._runtime = "as"  # noqa: SLF001

    first = backend.capabilities()
    second = backend.capabilities()
    assert first == second


# ---------------------------------------------------------------------------
# ``prefer`` override (DESIGN_backends_hardening.md §1.2)
# ---------------------------------------------------------------------------


def test_prefer_as_bypasses_probe() -> None:
    """Explicit ``prefer="as"`` wins over the probe — no subprocess spawn."""
    with patch(
        "orchestratord_codex.backend._detect_runtime",
        side_effect=AssertionError("probe must not run when prefer is set"),
    ):
        backend = CodexBackend(prefer="as")
    assert backend.runtime == "as"
    caps = backend.capabilities()
    assert caps.streaming_deltas is True
    assert caps.interrupt is True
    assert caps.approval_hooks is True


def test_prefer_cli_bypasses_probe() -> None:
    """Explicit ``prefer="cli"`` forces the Cli capability set."""
    with patch(
        "orchestratord_codex.backend._detect_runtime",
        side_effect=AssertionError("probe must not run when prefer is set"),
    ):
        backend = CodexBackend(prefer="cli")
    assert backend.runtime == "cli"
    caps = backend.capabilities()
    assert caps.resumable is True
    assert caps.streaming_deltas is False


def test_prefer_invalid_value_raises() -> None:
    with pytest.raises(ValueError):
        CodexBackend(prefer="bogus")  # type: ignore[arg-type]


def test_env_var_forces_runtime(monkeypatch) -> None:
    """``ORCHESTRATORD_CODEX_PREFER=as`` wins over the probe result."""
    monkeypatch.setenv("ORCHESTRATORD_CODEX_PREFER", "as")
    with patch("orchestratord_codex.backend._detect_runtime", return_value="cli"):
        backend = CodexBackend()
    assert backend.runtime == "as"


def test_invalid_env_var_falls_back_to_probe(monkeypatch) -> None:
    """A non-``cli``/``as`` env value is ignored and the probe runs."""
    monkeypatch.setenv("ORCHESTRATORD_CODEX_PREFER", "bogus")
    with patch("orchestratord_codex.backend._detect_runtime", return_value="cli"):
        backend = CodexBackend()
    assert backend.runtime == "cli"


def test_create_session_respects_prefer() -> None:
    """A ``prefer``-constructed backend picks the matching session class."""
    backend = CodexBackend(prefer="as")
    assert isinstance(backend.create_session(_spec()), CodexAppServerSession)
    backend = CodexBackend(prefer="cli")
    assert isinstance(backend.create_session(_spec()), CodexSession)


# ---------------------------------------------------------------------------
# Detect-runtime end-to-end (live subprocess; skipped if codex absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex binary not present on PATH",
)
def test_detect_runtime_with_real_codex() -> None:
    """If a real ``codex`` binary is on PATH, the probe produces a
    deterministic answer — used as a smoke test, not a correctness
    assertion.
    """
    runtime = _detect_runtime()
    assert runtime in ("cli", "as")
