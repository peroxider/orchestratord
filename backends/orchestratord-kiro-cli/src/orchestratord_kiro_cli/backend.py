"""KiroBackend — Cli backend wrapping the ``kiro`` binary.

Family: Cli
Capabilities: parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_kiro_cli.session import KiroSession


class KiroBackend:
    """Cli backend that spawns ``kiro`` per turn.

    The kiro CLI's event stream shape is not yet exercised in-tree
    (FEATURE_GAP §8.1). Until a wire-level translator is built, the
    backend buffers the entire stdout as a single TEXT event — the
    orchestrator's split-whole-text-into-pseudo-deltas degradation path
    handles downstream consumers uniformly.
    """

    name = "kiro-cli"
    display_name = "AWS Kiro (Cli)"

    def __init__(self) -> None:
        self._sessions: list[KiroSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify kiro is on PATH before daemon startup."""
        if shutil.which("kiro") is None:
            raise RuntimeError(
                "kiro executable was not found on PATH. Install the AWS Kiro "
                "CLI and complete its authentication before using this backend."
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
            # kiro has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = KiroSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
