"""Phase 3: AgentRunner Protocol injection unit tests.

Covers the three new kw-only constructor parameters
(``agent_runtime``, ``session_storage``, ``coordinator_provider``)
and the lazy default-resolution path.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from orchestratord.agent_runner import AgentRunner
from orchestratord.config.schema import AgentConfig, SandboxConfig, WorkspaceConfig


@dataclass(frozen=True)
class StubAgentRuntime:
    """Test-only runtime satisfying the AgentRuntime Protocol structurally."""

    marker: str = "stub-runtime"

    async def stream(self, **kwargs: Any) -> Any:
        return AsyncMock()

    async def resume(self, **kwargs: Any) -> Any:
        return AsyncMock()


@dataclass(frozen=True)
class StubSessionStorage:
    """Test-only session storage satisfying the SessionStorage Protocol."""

    marker: str = "stub-storage"

    def save(self, session_id: str, conversation: Any) -> None:
        return None

    def load(self, session_id: str) -> Any | None:
        return None

    def list_sessions(self, workspace: Path | None = None) -> list[Any]:
        return []

    def session_dir(self) -> Path:
        return Path("/tmp/stub-sessions")


@dataclass(frozen=True)
class StubCoordinator:
    """Test-only coordinator context provider."""

    marker: str = "stub-coordinator"
    _active: bool = False

    def is_active(self) -> bool:
        return self._active

    def enter(self, enabled: bool = True) -> Any:
        class _CM:
            def __enter__(self) -> "_CM":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

        return _CM()


@pytest.fixture
def runner_configs() -> tuple[AgentConfig, SandboxConfig, WorkspaceConfig]:
    return AgentConfig(), SandboxConfig(), WorkspaceConfig()


def test_default_construction_requires_explicit_backend(
    runner_configs: tuple[AgentConfig, SandboxConfig, WorkspaceConfig],
) -> None:
    """The core never silently selects an optional backend."""
    agent_cfg, sandbox_cfg, workspace_cfg = runner_configs
    with pytest.raises(ValueError, match="explicit AgentBackend"):
        AgentRunner(agent_cfg, sandbox_cfg, workspace_cfg)


def test_legacy_protocol_args_do_not_bypass_backend_boundary(
    runner_configs: tuple[AgentConfig, SandboxConfig, WorkspaceConfig],
) -> None:
    """If any kw arg is provided, _resolve_protocols() remains a no-op."""
    agent_cfg, sandbox_cfg, workspace_cfg = runner_configs
    runtime = StubAgentRuntime()
    storage = StubSessionStorage()
    coordinator = StubCoordinator()

    with pytest.raises(ValueError, match="explicit AgentBackend"):
        AgentRunner(
            agent_cfg,
            sandbox_cfg,
            workspace_cfg,
            agent_runtime=runtime,
            session_storage=storage,
            coordinator_provider=coordinator,
        )


def test_default_construction_has_no_lazy_backend_resolution(
    runner_configs: tuple[AgentConfig, SandboxConfig, WorkspaceConfig],
) -> None:
    """A default-constructed runner cannot resolve an optional backend."""
    agent_cfg, sandbox_cfg, workspace_cfg = runner_configs
    with pytest.raises(ValueError, match="explicit AgentBackend"):
        AgentRunner(agent_cfg, sandbox_cfg, workspace_cfg)


def test_partial_legacy_injection_does_not_bypass_backend_boundary(
    runner_configs: tuple[AgentConfig, SandboxConfig, WorkspaceConfig],
) -> None:
    """Partial injection is allowed: provided params are stored, and defaults
    are skipped for the whole protocol group (all-or-nothing resolution)."""
    agent_cfg, sandbox_cfg, workspace_cfg = runner_configs
    runtime = StubAgentRuntime()
    with pytest.raises(ValueError, match="explicit AgentBackend"):
        AgentRunner(
            agent_cfg,
            sandbox_cfg,
            workspace_cfg,
            agent_runtime=runtime,
        )
