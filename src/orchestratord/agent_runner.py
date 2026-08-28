"""Backward-compatible name for the backend-neutral runner.

Agent execution belongs to an :class:`AgentBackend`.  The former module
contained a ClawCodex-specific implementation; it is intentionally gone.
"""

from __future__ import annotations

from typing import Any

from .backend_runner import BackendRunner
from .runner_utils import _apply_pause_session, _apply_resume_session
from .session_state import AgentSession, RetryItem


class AgentRunner(BackendRunner):
    """Compatibility wrapper requiring an explicit backend."""

    def __init__(self, *args: Any, backend: Any = None, **kwargs: Any) -> None:
        if backend is None:
            raise ValueError(
                "AgentRunner requires an explicit AgentBackend; "
                "orchestratord no longer selects a concrete backend implicitly."
            )
        super().__init__(backend=backend, *args, **kwargs)

    @staticmethod
    def _apply_pause_session(session: AgentSession, reason: str = "operator_interrupt") -> None:
        _apply_pause_session(session, reason)

    @staticmethod
    def _apply_resume_session(session: AgentSession) -> None:
        _apply_resume_session(session)


__all__ = ["AgentRunner", "AgentSession", "RetryItem"]
