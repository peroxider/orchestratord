"""CursorBackend — Cli backend wrapping the ``cursor-agent`` binary.

Family: Cli
Capabilities: parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_cursor.session import CursorSession


class CursorBackend:
    """Cli backend that spawns ``cursor-agent`` per turn.

    The CLI's ``--output-format stream-json`` stream is translated
    wire-level by :class:`CursorSession` — ported from the multica Go
    reference (``server/pkg/agent/cursor.go``). Text arrives as complete
    blocks inside ``assistant`` events (no incremental text-fragment
    event exists in the protocol), so the backend does NOT claim
    ``streaming_deltas``.
    """

    name = "cursor"
    display_name = "Cursor (Cli)"

    def __init__(self) -> None:
        self._sessions: list[CursorSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify cursor-agent is on PATH before daemon startup."""
        if shutil.which("cursor-agent") is None:
            raise RuntimeError(
                "cursor-agent executable was not found on PATH. Install "
                "Cursor CLI and complete its provider configuration before "
                "using this backend."
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
            # cursor-agent has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = CursorSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()