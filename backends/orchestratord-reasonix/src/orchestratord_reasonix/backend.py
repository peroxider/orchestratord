"""ReasonixBackend — Cli backend wrapping the ``reasonix`` binary.

Family: Cli
Capabilities: parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_reasonix.session import ReasonixSession


class ReasonixBackend:
    """Cli backend that spawns ``reasonix`` per turn.

    The reasonix CLI's event stream shape is not yet exercised in-tree
    (FEATURE_GAP §8.1). Until a wire-level translator is built, the
    backend buffers the entire stdout as a single TEXT event — the
    orchestrator's split-whole-text-into-pseudo-deltas degradation path
    handles downstream consumers uniformly.
    """

    name = "reasonix"
    display_name = "Reasonix (Cli)"

    def __init__(self) -> None:
        self._sessions: list[ReasonixSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify reasonix is on PATH before daemon startup."""
        if shutil.which("reasonix") is None:
            raise RuntimeError(
                "reasonix executable was not found on PATH. Install the "
                "Reasonix CLI and complete its authentication before using "
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
            # reasonix has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = ReasonixSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
