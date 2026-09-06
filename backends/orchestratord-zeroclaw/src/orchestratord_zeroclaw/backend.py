"""ZeroclawBackend — Cli backend wrapping the ``zeroclaw`` binary.

Family: Cli
Capabilities: parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_zeroclaw.session import ZeroclawSession


class ZeroclawBackend:
    """Cli backend that spawns ``zeroclaw`` per turn.

    The zeroclaw CLI's event stream shape is not yet exercised in-tree
    (FEATURE_GAP §8.1). Until a wire-level translator is built, the
    backend buffers the entire stdout as a single TEXT event — the
    orchestrator's split-whole-text-into-pseudo-deltas degradation path
    handles downstream consumers uniformly.
    """

    name = "zeroclaw"
    display_name = "ZeroClaw (Cli)"

    def __init__(self) -> None:
        self._sessions: list[ZeroclawSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify zeroclaw is on PATH before daemon startup."""
        if shutil.which("zeroclaw") is None:
            raise RuntimeError(
                "zeroclaw executable was not found on PATH. Install the "
                "ZeroClaw CLI and complete its authentication before using "
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
            # zeroclaw has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = ZeroclawSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
