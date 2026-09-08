"""ZeroclawBackend — Cli backend wrapping the ``zeroclaw`` binary.

Family: Cli
Capabilities: parallel_sessions, streaming_deltas
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession
from orchestratord_zeroclaw.session import ZeroclawSession


class ZeroclawBackend:
    """Cli backend that speaks ZeroClaw's real ACP wire format.

    Each turn spawns ``zeroclaw acp`` and drives the ACP JSON-RPC 2.0
    handshake (initialize → session/new | session/resume → session/prompt)
    over NDJSON stdio, translating ``agent_message_chunk`` into
    TEXT_DELTA and ``tool_call`` / ``tool_call_update`` into TOOL_CALL /
    TOOL_RESULT (FEATURE_GAP §8.2.3, ported from the Go reference).
    """

    name = "zeroclaw"
    display_name = "ZeroClaw (Cli)"

    def __init__(self) -> None:
        self._sessions: list[ZeroclawSession] = []

    def preflight(self, spec: SessionSpec) -> None:
        """Verify zeroclaw is on PATH before daemon startup."""
        if shutil.which("zeroclaw") is None:
            raise RuntimeError(
                "zeroclaw executable was not found on PATH. Install the "
                "ZeroClaw CLI and complete its authentication before using "
                "this backend."
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            # zeroclaw has no cross-process resume probe; the wire-level
            # session/resume is driven from the persisted session id
            # without a pre-probe (probe_resume stays UNDETECTABLE).
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = ZeroclawSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
