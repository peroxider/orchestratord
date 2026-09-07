"""KimiBackend — ACP backend wrapping ``kimi acp``.

Family: Cli (ACP JSON-RPC stdio transport)
Capabilities: streaming_deltas parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_kimi.session import KimiSession


class KimiBackend:
    """ACP backend that spawns ``kimi acp`` per turn.

    Ported from multica ``server/pkg/agent/kimi.go``: Kimi Code CLI
    speaks ACP (Agent Client Protocol) JSON-RPC 2.0 over stdio via the
    ``acp`` subcommand, so the session translates ``agent_message_chunk``
    / ``tool_call`` / ``tool_call_update`` updates into real deltas and
    tool events instead of buffering whole stdout (FEATURE_GAP §8.2.3).
    Permissions are auto-answered in-protocol, mirroring the unattended
    Go daemon.
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
            streaming_deltas=True,   # session/update agent_message_chunk
            resumable=False,         # continuity via session/resume per turn
            interrupt=False,         # best-effort session/cancel only
            approval_hooks=False,    # permissions auto-answered in-protocol
            parallel_sessions=True,  # one process per turn
            cost_reporting=False,    # no usage over ACP (wire-log scan in Go)
            tool_filtering=False,
            takeover=False,
            goal_mode=False,
            # kimi has no cross-process resume probe.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = KimiSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()