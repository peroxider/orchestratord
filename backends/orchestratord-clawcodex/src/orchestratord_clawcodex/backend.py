"""ClawcodexBackend — InProcess backend wrapping clawcodex QueryRunner.

Family: InProcess
Capabilities: streaming_deltas approval_hooks cost_reporting tool_filtering takeover goal_mode resume_detection goal_mode

This is the reference InProcess backend.  It imports ``extensions.api.query``
directly (the ONLY place in the orchestratord ecosystem outside of
``orchestratord.adapters.clawcodex`` where this is allowed — it is the
backend's responsibility to bridge the clawcodex-specific types into the SPI).

Per the strangler-fig migration (§6.1):
- Phase B (current): this backend wraps QueryRunner directly
- Phase C: freezes as shim, forwarding to orchestratord
- Phase D: deleted, replaced by entry_points registration
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_clawcodex.session import ClawcodexSession


class ClawcodexBackend:
    """InProcess backend that wraps the clawcodex QueryRunner."""

    name = "clawcodex"
    display_name = "ClawCodex (InProcess)"

    def __init__(self) -> None:
        self._sessions: list[ClawcodexSession] = []

    def preflight(self, spec: SessionSpec) -> None:
        """Validate the clawcodex runtime and configured provider locally."""
        source = os.environ.get(
            "CLAWCODEX_SOURCE", "/mnt/c/WorkSpace/AgentSDK/clawcodex-ascend"
        )
        if not Path(source).is_dir():
            raise RuntimeError(
                f"CLAWCODEX_SOURCE source directory does not exist: {source}. "
                "Set it to the clawcodex-ascend source directory."
            )
        if source not in sys.path:
            sys.path.insert(0, source)
        try:
            query = importlib.import_module("extensions.api.query")
        except ImportError as exc:
            raise RuntimeError(
                f"cannot import extensions.api.query from CLAWCODEX_SOURCE: {exc}"
            ) from exc
        if not all(hasattr(query, name) for name in ("QueryConfig", "QueryRunner")):
            raise RuntimeError(
                "extensions.api.query must export QueryConfig and QueryRunner."
            )

        provider = (spec.provider or "").strip()
        if not provider:
            raise RuntimeError("agent.provider must be configured for clawcodex.")
        try:
            config = importlib.import_module("src.config").get_provider_config(provider)
        except (ImportError, ValueError) as exc:
            raise RuntimeError(
                f"clawcodex provider '{provider}' is not configured: {exc}"
            ) from exc
        if not config.get("api_key"):
            raise RuntimeError(
                f"No API key configured for provider '{provider}'. Run "
                "'clawcodex-dev login' or configure its provider API key."
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=False,
            approval_hooks=True,
            parallel_sessions=False,
            cost_reporting=True,
            tool_filtering=True,
            takeover=True,
            goal_mode=True,
            # ADR-003: clawcodex exposes a transcript probe via
            # QueryRunner.probe_transcript (see session.py:probe_resume).
            resume_detection=True,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = ClawcodexSession(spec)
        self._sessions.append(session)
        return session

    def get_task_registry(self):
        """Return a new RuntimeTaskRegistry for real-time message injection."""
        try:
            from clawcodex_ext.task_registry import RuntimeTaskRegistry
            return RuntimeTaskRegistry()
        except ImportError:
            return None

    def dispose(self) -> None:
        for s in self._sessions:
            try:
                s.close_sync()
            except Exception:
                pass
        self._sessions.clear()
