"""Tests for the AgentTaskRunner Protocol structure.

Per ``DESIGN_agent_task_abstraction.md`` §4 / §16: the protocol is
satisfied structurally (not via inheritance) by the runner implementations,
and the ``run_task`` signature matches the documented contract.
"""

from __future__ import annotations

import inspect

from orchestratord.agent.runner import AgentTaskRunner
from orchestratord.backend_runner import BackendRunner
from orchestratord.orchestrator import Orchestrator


class _FakeRunner:
    """Minimal structural stand-in for a capability-layer runner."""

    async def run_task(self, task, *, progress_callback=None, diagnostics_callback=None):
        return task


def test_runner_is_runtime_checkable():
    """AgentTaskRunner is a Protocol, not an ABC."""
    assert hasattr(AgentTaskRunner, "_is_protocol")
    assert hasattr(AgentTaskRunner, "_is_runtime_protocol")
    # The protocol class itself defines ``run_task`` (structural contract).
    assert AgentTaskRunner.__dict__["run_task"] is not None


def test_fake_runner_satisfies_protocol():
    """Structural (duck-typed) satisfaction — no inheritance required."""
    runner = _FakeRunner()
    assert callable(getattr(runner, "run_task", None))
    assert inspect.iscoroutinefunction(runner.run_task)


def test_backend_runner_satisfies_protocol():
    """BackendRunner structurally implements the protocol (single SPI runner)."""
    from orchestratord.config.schema import AgentConfig, SandboxConfig

    runner = BackendRunner(
        backend=object(),  # type: ignore[arg-type]
        agent_config=AgentConfig(),
        sandbox_config=SandboxConfig(),
    )
    assert callable(getattr(runner, "run_task", None))
    assert inspect.iscoroutinefunction(runner.run_task)


def test_run_task_signature_contract():
    """run_task accepts (task, *, progress_callback, diagnostics_callback)."""
    sig = inspect.signature(BackendRunner.run_task)
    params = sig.parameters
    assert "task" in params
    assert params["task"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert "progress_callback" in params
    assert "diagnostics_callback" in params
    # Both callbacks are keyword-only.
    assert params["progress_callback"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["diagnostics_callback"].kind is inspect.Parameter.KEYWORD_ONLY


def test_orchestrator_exposes_task_entry_points():
    """Orchestrator exposes the Layer-1 + public Layer-2 task API (§9)."""
    assert callable(getattr(Orchestrator, "_resolve_runner", None))
    assert callable(getattr(Orchestrator, "_run_agent_task", None))
    assert callable(getattr(Orchestrator, "run_task", None))
    # Public entry point must be a coroutine function.
    assert inspect.iscoroutinefunction(Orchestrator.run_task)


def test_orchestrator_resolves_runner_to_protocol_implementer():
    """The orchestrator's runner selection honours the AgentTaskRunner contract."""
    src = inspect.getsource(Orchestrator._resolve_runner)
    # The implementation prefers candidates that implement ``run_task``.
    assert "run_task" in src
    assert "agent_runner" in src
