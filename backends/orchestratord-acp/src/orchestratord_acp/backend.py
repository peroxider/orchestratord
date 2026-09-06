"""AcpBackend — generic ACP (Agent Client Protocol) backend factory.

Family: Protocol
Capabilities: streaming_deltas interrupt approval_hooks parallel_sessions

One :class:`AcpBackend` class drives every ACP runtime. The concrete
runtime identity (grok / codebuddy / qwenpaw) is selected at construction
via the descriptor's ``prefer`` hint — the same mechanism codex uses for
its ``codex-cli`` / ``codex-app-server`` split (FEATURE_GAP §8.3 /
DESIGN_backends_hardening.md §1.2).
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_acp.runtime import (
    AcpRuntime,
    resolve_binary,
    resolve_runtime,
)
from orchestratord_acp.session import AcpSession


class AcpBackend:
    """Protocol backend that spawns one ACP stdio subprocess per session."""

    def __init__(self, prefer: str | None = None) -> None:
        self._runtime: AcpRuntime = resolve_runtime(prefer)
        self._sessions: list[AcpSession] = []

    @property
    def name(self) -> str:
        return self._runtime.id

    @property
    def display_name(self) -> str:
        return self._runtime.display_name

    @property
    def runtime(self) -> AcpRuntime:
        """The resolved ACP runtime (diagnostics / tests)."""
        return self._runtime

    def preflight(self, spec: SessionSpec) -> None:
        """Verify the runtime's ACP binary is available on PATH."""
        binary = resolve_binary(self._runtime, spec.runtime_bin)
        if shutil.which(binary) is None:
            raise RuntimeError(
                f"{binary!r} executable was not found on PATH. Install the "
                f"{self._runtime.display_name} runtime and complete its "
                "authentication before using this backend."
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,   # session/update agent_message_chunk
            resumable=False,         # ephemeral stdio subprocess
            interrupt=True,          # session/cancel
            approval_hooks=True,     # session/request_permission
            parallel_sessions=True,  # one process per session
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            goal_mode=False,
            resume_detection=False,  # no cross-process probe path
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = AcpSession(spec, self._runtime)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
