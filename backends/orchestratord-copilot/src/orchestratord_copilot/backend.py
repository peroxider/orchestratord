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

    The CLI's ``--output-format json`` stream (JSONL events with
    ``assistant.message_delta`` fragments) is translated wire-level by
    :class:`CopilotSession` — ported from the multica Go reference
    (``server/pkg/agent/copilot.go``). Because the stream carries genuine
    incremental text deltas, the backend claims ``streaming_deltas``.
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
            streaming_deltas=True,
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