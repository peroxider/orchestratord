"""OpenCodeBackend — Protocol backend connecting to opencode serve.

Family: Protocol
Capabilities: streaming_deltas approval_hooks parallel_sessions goal_mode goal_mode
"""

from __future__ import annotations

import importlib
import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_opencode.session import OpenCodeSession


class OpenCodeBackend:
    """Protocol backend that manages opencode serve subprocesses.

    Each session starts its own opencode serve instance (process-level
    isolation).  The backend tracks subprocesses for cleanup on dispose.
    """

    name = "opencode"
    display_name = "OpenCode (Protocol)"

    def __init__(self) -> None:
        self._sessions: list[OpenCodeSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify OpenCode and its HTTP transport are available locally."""
        if shutil.which("opencode") is None:
            raise RuntimeError(
                "opencode executable was not found on PATH. Install OpenCode "
                "and complete its provider configuration before using this backend."
            )
        try:
            __import__("httpx")
        except ImportError as exc:
            raise RuntimeError(
                "httpx is required by the opencode backend but is not installed."
            ) from exc

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=False,
            approval_hooks=True,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            # opencode serve exposes session/load HTTP probe
            # (see session.py:probe_resume).
            resume_detection=True,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = OpenCodeSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        for s in self._sessions:
            try:
                s.close_sync()
            except Exception:
                pass
        self._sessions.clear()
