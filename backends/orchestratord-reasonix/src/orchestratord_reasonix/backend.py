"""ReasonixBackend — ACP backend wrapping ``reasonix acp``.

Family: Cli (ACP JSON-RPC stdio transport)
Capabilities: streaming_deltas parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_reasonix.session import ReasonixSession


class ReasonixBackend:
    """ACP backend that spawns ``reasonix acp`` per turn.

    Ported from multica ``server/pkg/agent/reasonix.go``: the Reasonix
    CLI speaks ACP (Agent Client Protocol) JSON-RPC 2.0 over stdio via
    the ``acp`` subcommand (fixed sandbox/profile flags), so the session
    translates ``agent_message_chunk`` / ``tool_call`` /
    ``tool_call_update`` updates into real deltas and tool events
    instead of buffering whole stdout (FEATURE_GAP §8.2.3).  Permissions
    — including Reasonix user questions and protected decisions — are
    auto-answered in-protocol, mirroring the unattended Go daemon.
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
            streaming_deltas=True,   # session/update agent_message_chunk
            resumable=False,         # continuity via session/resume per turn
            interrupt=False,         # best-effort session/cancel only
            approval_hooks=False,    # permissions auto-answered in-protocol
            parallel_sessions=True,  # one process per turn
            cost_reporting=False,    # status_update usage has no SPI surface
            tool_filtering=False,
            takeover=False,
            goal_mode=False,
            # reasonix has no cross-process resume probe.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = ReasonixSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
