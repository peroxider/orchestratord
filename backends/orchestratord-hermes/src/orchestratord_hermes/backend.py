"""HermesBackend — Cli(venv) backend wrapping hermes CLI.

Family: Cli
Capabilities: resumable parallel_sessions goal_mode goal_mode
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_hermes.session import HermesSession


class HermesBackend:
    """Cli backend that spawns ``hermes`` per turn.

    Hermes is a Python agent (Nous Research) with its own dependency
    lock discipline — venv isolation is recommended for production use.
    """

    name = "hermes"
    display_name = "Hermes (Cli/venv)"

    def __init__(self) -> None:
        self._sessions: list[HermesSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify the Hermes executable is installed before daemon startup."""
        if shutil.which("hermes") is None:
            raise RuntimeError(
                "hermes executable was not found on PATH. Install Hermes and "
                "complete its provider configuration before using this backend."
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=False,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            # hermes has no resume probe — backend declares
            # unsupported (REJECTED). resume_detection stays False.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = HermesSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
