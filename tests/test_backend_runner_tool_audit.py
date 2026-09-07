"""The TOOL_CALL post-hoc policy audit must be skipped for
``approval_hooks`` backends.

Their pre-execution APPROVAL_REQUEST gate is authoritative; every
TOOL_CALL envelope arrives after the tool already executed server-side,
so a post-hoc "denied" line is false (the tool did run) and pure noise
under the default ask policy.
"""

from __future__ import annotations

from typing import Any

from orchestratord.backend_runner import BackendRunner
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind


def _event() -> EventEnvelope:
    return EventEnvelope(
        seq=1,
        timestamp=0.0,
        kind=EventKind.TOOL_CALL,
        payload={
            "call_id": "c1",
            "name": "bash",
            "arguments": {"command": "ls"},
        },
    )


def _runner(approval_hooks: bool) -> tuple[BackendRunner, list[Any]]:
    runner = object.__new__(BackendRunner)

    class _Backend:
        name = "fake"

        def capabilities(self) -> BackendCapabilities:
            return BackendCapabilities(approval_hooks=approval_hooks)

    runner.backend = _Backend()

    evaluated: list[Any] = []

    class _Policy:
        def evaluate(self, event: Any, context: Any) -> bool:
            evaluated.append(event)
            event.deny(reason="policy=ask (not supported in autonomous mode)")
            return False

    runner._approval_policy = _Policy()
    return runner, evaluated


def test_audit_skipped_for_approval_hooks_backend() -> None:
    runner, evaluated = _runner(approval_hooks=True)
    runner._handle_tool_call_envelope(_event(), {})
    assert evaluated == [], (
        "approval_hooks backends gate pre-execution — the post-hoc "
        "TOOL_CALL audit must not run"
    )


def test_audit_runs_for_plain_backend() -> None:
    runner, evaluated = _runner(approval_hooks=False)
    runner._handle_tool_call_envelope(_event(), {})
    assert len(evaluated) == 1
    assert evaluated[0].tool_name == "bash"


def test_audit_skips_gracefully_when_capabilities_probe_fails() -> None:
    """A backend whose capabilities() raises must not break the event
    loop — the conservative path is to run the audit.
    """

    class _BrokenBackend:
        name = "broken"

        def capabilities(self) -> Any:
            raise RuntimeError("boom")

    runner = object.__new__(BackendRunner)
    runner.backend = _BrokenBackend()
    evaluated: list[Any] = []

    class _Policy:
        def evaluate(self, event: Any, context: Any) -> bool:
            evaluated.append(event)
            event.allow("policy=never")
            return True

    runner._approval_policy = _Policy()
    runner._handle_tool_call_envelope(_event(), {})
    assert len(evaluated) == 1
