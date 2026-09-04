"""QwenBackend — Cli backend wrapping the ``qwen`` binary.

Family: Cli
Capabilities: streaming_deltas parallel_sessions
"""

from __future__ import annotations

import shutil

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_qwen.session import QwenSession


class QwenBackend:
    """Cli backend that spawns ``qwen -p --output-format stream-json`` per turn.

    Unlike the conservative cursor/copilot/kimi backends, qwen's wire
    format is documented as streaming JSON (FEATURE_GAP §8.1: ``qwen -p
    --output-format stream-json``). The session reads NDJSON line-by-line
    and emits ``TEXT_DELTA`` events as ``content_block_delta`` frames
    arrive, satisfying the ``streaming_deltas=True`` capability bit.
    """

    name = "qwen"
    display_name = "Qwen (Cli/stream-json)"

    def __init__(self) -> None:
        self._sessions: list[QwenSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify qwen is on PATH before daemon startup."""
        if shutil.which("qwen") is None:
            raise RuntimeError(
                "qwen executable was not found on PATH. Install Qwen CLI "
                "(DashScope) and complete its provider configuration "
                "before using this backend."
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
            # qwen has no cross-process resume protocol.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = QwenSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()