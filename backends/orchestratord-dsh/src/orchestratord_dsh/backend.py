"""DshBackend — SdkProcess backend wrapping deepseek-harness-sdk.

"""

from __future__ import annotations

import importlib
import logging
import os

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession
from orchestratord_dsh.session import DshSession

logger = logging.getLogger(__name__)


class DshBackend:
    """SdkProcess backend that manages a DeepSeek Harness subprocess.

    Each ``create_session()`` spawns one harness process (1:1 mapping
    because the SDK's provider/model are process-level, not per-session).

    Protocol-family metadata is declared in ``descriptor.py``.
    """

    name = "dsh"
    display_name = "DeepSeek Harness (SdkProcess/Cli)"

    # Providers the DeepSeek Harness runtime ships adapters for. A
    # provider outside this set can never work — fail at preflight with
    # an actionable message instead of mid-stage with the runtime's
    # opaque "no adapter registered" error.
    SUPPORTED_PROVIDERS = frozenset({"deepseek-official"})

    def __init__(self) -> None:
        self._sessions: list[DshSession] = []

    def preflight(self, spec: SessionSpec) -> None:
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
        # Reject providers the runtime cannot serve, with a message
        # that points at the real fix (agent.provider in the config file).
        provider = (getattr(spec, "provider", None) or "").strip()
        if provider and provider not in self.SUPPORTED_PROVIDERS:
            raise RuntimeError(
                f"provider '{provider}' has no adapter in the DeepSeek "
                "Harness runtime. Backend dsh requires "
                "agent.provider: deepseek-official — set it in the "
                "--config file (agent.provider) or remove the override "
                "so the backend default applies."
            )
        # Verify the credential source up front. The SDK runtime
        # inherits the caller's environment (DEEPSEEK_API_KEY), or the
        # spec can carry an explicit key — including "$VAR" references,
        # which must resolve now rather than failing mid-run.
        api_key = (getattr(spec, "api_key", None) or "").strip()
        if api_key.startswith("$"):
            env_name = api_key[1:].strip("{} ")
            if not os.environ.get(env_name):
                raise RuntimeError(
                    f"api_key reference '{api_key}' cannot be resolved — "
                    f"{env_name} is not set in the environment. Export it "
                    "or set agent.api_key in the --config file."
                )
        elif not api_key and not os.environ.get("DEEPSEEK_API_KEY"):
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set and no api_key is configured. "
                "Set the DEEPSEEK_API_KEY environment variable (the SDK "
                "runtime inherits the caller's environment) or set "
                "agent.api_key in the --config file."
            )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,
            # Honesty: see session.py — cross-process resume is
            # not supported by the SDK runtime (id collision).
            resumable=False,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=False,
            takeover=False,
            # DSH SDK offers no resume probe; the orchestrator
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
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.warning("dsh session close failed: %s", exc)
        self._sessions.clear()
