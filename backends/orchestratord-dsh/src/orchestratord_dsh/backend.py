"""DshBackend — SdkProcess backend wrapping deepseek-harness-sdk.

Family: Cli
Capabilities: resumable parallel_sessions cost_reporting goal_mode goal_mode
"""

from __future__ import annotations

import importlib

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_dsh.session import DshSession


class DshBackend:
    """SdkProcess backend that manages a DeepSeek Harness subprocess.

    Each ``create_session()`` spawns one harness process (1:1 mapping
    because the SDK's provider/model are process-level, not per-session).

    Note: the SPI family classifier (``_classify_family``) returns
    ``Cli`` because the reported bits do not satisfy the SdkProcess
    heuristic. We mirror that here so the drift detector stays green;
    a future SDK release that lights up ``interrupt + approval_hooks +
    streaming_deltas`` would justify re-elevating the Family to
    SdkProcess.
    """

    name = "dsh"
    display_name = "DeepSeek Harness (SdkProcess/Cli)"

    def __init__(self) -> None:
        self._sessions: list[DshSession] = []

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify the DeepSeek Harness SDK exposes its required API."""
        try:
            api = importlib.import_module("deepseek_harness.api")
        except ImportError as exc:
            raise RuntimeError(
                "deepseek_harness is not installed. Install the DeepSeek Harness "
                "SDK before using this backend."
            ) from exc
        missing = [
            name
            for name in ("DeepSeekHarness", "DeepSeekHarnessConfig")
            if not hasattr(api, name)
        ]
        if missing:
            raise RuntimeError(
                "deepseek_harness.api is missing required exports: "
                + ", ".join(missing)
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=False,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=False,
            takeover=False,
            # ADR-003: DSH SDK offers no resume probe; the orchestrator
            # must treat this as UNDETECTABLE (see session.py:probe_resume).
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = DshSession(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        for s in self._sessions:
            try:
                s.close_sync()
            except Exception:
                pass
        self._sessions.clear()
