"""OpenclawBackend — Cli backend wrapping the ``openclaw`` binary.

Family: Cli
Capabilities: parallel_sessions

§8.1 lists openclaw's native protocol as HTTP (``openclaw agent --local``
vs Gateway routing). That wire path is not yet exercised in-tree, so this
backend is conservative — spawn-per-turn Cli buffering stdout as a single
TEXT event, deferring the HTTP/Gateway translator.
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_openclaw.session import OpenclawSession


class OpenclawBackend:
    """Cli backend that spawns ``openclaw`` per turn."""

    name = "openclaw"
    display_name = "OpenClaw (Cli)"

    def __init__(self) -> None:
        self._sessions: list[OpenclawSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify openclaw is on PATH before daemon startup."""
        if shutil.which("openclaw") is None:
            raise RuntimeError(
                "openclaw executable was not found on PATH. Install the "
                "OpenClaw CLI and complete its authentication before using "
                "this backend."
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
            # openclaw has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = OpenclawSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
