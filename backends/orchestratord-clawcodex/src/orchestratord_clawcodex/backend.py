"""ClawCodex backend factory, native preflight, and isolated SDK workers."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_clawcodex.process_session import ClawcodexProcessSession


class ClawcodexBackend:
    """Run each ClawCodex conversation in a separately owned Python process."""

    name = "clawcodex"
    display_name = "ClawCodex (isolated SDK)"

    def __init__(self) -> None:
        self._sessions: list[ClawcodexProcessSession] = []

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
            query = __import__("extensions.api.query", fromlist=("query",))
        except ImportError as exc:
            raise RuntimeError(
                f"cannot import extensions.api.query from CLAWCODEX_SOURCE: {exc}"
            ) from exc
        if not all(hasattr(query, name) for name in ("QueryConfig", "QueryRunner")):
            raise RuntimeError(
                "extensions.api.query must export QueryConfig and QueryRunner."
            )
        if not (
            hasattr(query, "ApprovalRequestEvent")
            and callable(getattr(query.QueryRunner, "approve", None))
            and callable(getattr(query.QueryRunner, "cancel_pending_approvals", None))
        ):
            raise RuntimeError(
                "ClawCodex approval bridge is incomplete. The configured "
                "CLAWCODEX_SOURCE must expose ApprovalRequestEvent and "
                "QueryRunner.approve/cancel_pending_approvals; update the "
                "AgentSDK query bridge before starting this backend."
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
        if provider == "openai-codex":
            auth = importlib.import_module("src.auth.codex_oauth")
            if not auth.get_codex_auth_status(include_cli=True).is_authenticated:
                raise RuntimeError(
                    "No OAuth credentials configured for openai-codex. Run "
                    "'clawcodex-dev login' and select openai-codex."
                )
            return
        if not config.get("api_key") and not importlib.import_module(
            "src.auth.auth"
        ).load_api_key(provider):
            raise RuntimeError(
                f"No API key configured for provider '{provider}'. Run "
                "'clawcodex-dev login' or configure its provider API key."
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,
            resumable=True,
            interrupt=False,
            approval_hooks=True,
            parallel_sessions=False,
            cost_reporting=True,
            tool_filtering=True,
            takeover=True,
            goal_mode=True,
            # clawcodex exposes a transcript probe via
            # QueryRunner.probe_transcript (see session.py:probe_resume).
            # Upstream TEMP-DISABLED this bit because a missing SDK probe
            # would "probe and fail every run". That hazard no longer
            # exists: probe_resume() is dual-path (SDK probe_transcript
            # → session_storage directory check → UNDETECTABLE), so every
            # failure mode degrades gracefully and never kills a run.
            resume_detection=True,
            pausable=os.name == "posix",
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        session = ClawcodexProcessSession(spec, self.capabilities())
        self._sessions.append(session)
        return session

    def get_task_registry(self) -> None:
        """Native task registries stay in the worker; core followups use send()."""

    def dispose(self) -> None:
        for s in self._sessions:
            try:
                s.close_sync()
            except Exception:
                pass
        self._sessions.clear()
