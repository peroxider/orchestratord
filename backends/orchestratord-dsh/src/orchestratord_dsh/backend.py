"""DshBackend — SdkProcess backend wrapping deepseek-harness-sdk.

"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession
from orchestratord_dsh.cordis_gen import (
    CordisConfigError,
    probe_llm_pi_ai_available,
    resolve_route,
    resolve_route_credential,
    validate_providers,
)
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

    # Providers the stock runtime auto-mounts. With no ``agent.providers``
    # registry a provider outside this set can never work — fail at
    # preflight with an actionable message instead of mid-stage with the
    # runtime's opaque "no adapter registered" error. When a registry IS
    # configured the runtime mounts one llm-pi-ai adapter route per entry
    # (see cordis_gen.py) and any declared route name becomes valid.
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
        providers = dict((getattr(spec, "extra", {}) or {}).get("providers") or {})
        if providers:
            self._preflight_provider_routes(spec, providers)
            return
        self._preflight_legacy(spec)

    # ------------------------------------------------------------------
    # Route path: agent.providers registry configured
    # ------------------------------------------------------------------

    def _preflight_provider_routes(
        self, spec: SessionSpec, providers: dict[str, dict]
    ) -> None:
        # The registry owns route generation; an explicit cordis config
        # would be silently replaced (or, worse, half-composed with it)
        # — reject the ambiguous combination instead.
        if (getattr(spec, "cordis", None) or "").strip():
            raise RuntimeError(
                "agent.cordis and agent.providers are mutually exclusive — "
                "a custom cordis config owns its own adapter mounts, so "
                "provider route generation is disabled. Remove one of the two."
            )
        try:
            validate_providers(providers)
            provider, _model = resolve_route(
                providers,
                getattr(spec, "provider", None),
                getattr(spec, "model", None),
                default_model="deepseek-v4-flash",
            )
        except CordisConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        # The legacy stock adapter remains reachable alongside a registry
        # (initialize() auto-mounts it for deepseek-official); its
        # credential chain is checked by the legacy path.
        if provider == "deepseek-official":
            self._preflight_legacy(spec)
            return
        cfg = providers.get(provider) or {}
        try:
            resolve_route_credential(cfg.get("api_key"), {**os.environ, **spec.env})
        except CordisConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        self._probe_runtime_plugin(spec)

    def _probe_runtime_plugin(self, spec: SessionSpec) -> None:
        """Best-effort guard against runtime builds without llm-pi-ai.

        The scan can only inspect the BUNDLED runtime executable, so it
        is inconclusive — not negative — whenever the launch resolves
        outside it: a custom ``agent.runtime_bin`` or the dev-only node
        carrier (``DSH_RUNTIME_MODE=node``). Both skip the probe; an
        incompatible build surfaces at turn time with the runtime's own
        error.
        """
        if {**os.environ, **spec.env}.get("DSH_RUNTIME_MODE") == "node":
            return
        if (getattr(spec, "runtime_bin", None) or "").strip():
            return
        if not probe_llm_pi_ai_available():
            raise RuntimeError(
                "the installed DeepSeek Harness runtime does not ship the "
                "llm-pi-ai adapter plugin — custom provider routes "
                "(agent.providers) need a runtime build that includes "
                "@deepseek-ai/dsh-llm-pi-ai. Upgrade deepseek-harness-"
                "runtime-bin or point agent.runtime_bin at a newer build."
            )

    # ------------------------------------------------------------------
    # Legacy path: no agent.providers registry (deepseek-official only)
    # ------------------------------------------------------------------

    def _preflight_legacy(self, spec: SessionSpec) -> None:
        # Reject providers the runtime cannot serve, with a message
        # that points at the real fix (agent.provider in the config file).
        provider = (getattr(spec, "provider", None) or "").strip()
        if provider and provider not in self.SUPPORTED_PROVIDERS:
            if spec.cordis:
                # An explicit profile patch owns its plugin and authentication.
                # The runtime, not the generic adapter, validates that mount.
                if not spec.model:
                    raise RuntimeError("agent.model is required for a custom cordis provider.")
                if not Path(spec.cordis).expanduser().is_file():
                    raise RuntimeError(f"agent.cordis file does not exist: {spec.cordis}")
                return
            raise RuntimeError(
                f"provider '{provider}' has no adapter in the DeepSeek "
                "Harness runtime. Backend dsh requires "
                "agent.provider: deepseek-official — set it in the "
                "--config file (agent.provider) or remove the override "
                "so the backend default applies. To serve other providers, "
                "declare them under agent.providers or mount them via agent.cordis."
            )
        # Verify the credential source up front. The SDK runtime
        # inherits the caller's environment (DEEPSEEK_API_KEY), or the
        # spec can carry an explicit key — including "$VAR" references,
        # which must resolve now rather than failing mid-run.
        environ = {**os.environ, **spec.env}
        try:
            api_key = resolve_route_credential(spec.api_key, environ)
        except CordisConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        if not api_key and not environ.get("DEEPSEEK_API_KEY"):
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
            pausable=os.name == "posix",
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
