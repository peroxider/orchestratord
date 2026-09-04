"""KimiBackend — Cli backend wrapping the ``kimi`` binary.

Family: Cli
Capabilities: parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_kimi.session import KimiSession


class KimiBackend:
    """Cli backend that spawns ``kimi`` per turn.

    Kimi (Moonshot AI) ships a CLI that is friendly to Chinese-language
    prompts. Until a wire-level translator is exercised, the backend
    buffers the entire stdout as a single TEXT event — the orchestrator's
    split-whole-text-into-pseudo-deltas degradation path handles
    downstream consumers uniformly.
    """

    name = "kimi"
    display_name = "Kimi (Cli)"

    def __init__(self) -> None:
        self._sessions: list[KimiSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify kimi is on PATH before daemon startup."""
        if shutil.which("kimi") is None:
            raise RuntimeError(
                "kimi executable was not found on PATH. Install Kimi CLI "
                "and complete its provider configuration before using "
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
            # kimi has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = KimiSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()