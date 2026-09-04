"""CopilotBackend — Cli backend wrapping the ``copilot`` binary.

Family: Cli
Capabilities: parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_copilot.session import CopilotSession


class CopilotBackend:
    """Cli backend that spawns ``copilot`` per turn.

    The copilot CLI's event stream shape is not yet exercised in-tree
    (FEATURE_GAP §8.1: "事件流需实验"). Until a wire-level translator
    is built, the backend buffers the entire stdout as a single TEXT
    event — the orchestrator's split-whole-text-into-pseudo-deltas
    degradation path handles downstream consumers uniformly.
    """

    name = "copilot"
    display_name = "GitHub Copilot (Cli)"

    def __init__(self) -> None:
        self._sessions: list[CopilotSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify copilot is on PATH before daemon startup."""
        if shutil.which("copilot") is None:
            raise RuntimeError(
                "copilot executable was not found on PATH. Install "
                "GitHub Copilot CLI and complete its authentication "
                "before using this backend."
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=False,
            resumable=False,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            # copilot has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = CopilotSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()