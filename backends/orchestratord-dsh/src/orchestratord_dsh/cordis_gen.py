"""Cordis config generation for custom dsh provider routes.

The DeepSeek Harness runtime mounts LLM adapters as cordis plugins. The
bundled default config only mounts ``@deepseek-ai/dsh-llm-deepseek``
(hence the historical "deepseek-official only" limitation), but the
runtime also ships ``@deepseek-ai/dsh-llm-pi-ai`` — the same
configurable multi-provider adapter its web Models page uses. This
module surfaces that capability for orchestratord: it text-appends an
``llm-pi-ai`` plugin block (one route per ``agent.providers`` entry) to
the bundled default config.

Why text-append instead of YAML parse → emit: the bundled default uses
``!!js`` runtime tags (``process.env`` fallbacks) that no Python YAML
loader understands. Appending to the original text keeps those tags
byte-identical; the appended block contains only plain YAML nodes.
Both adapters then coexist — route keys never collide because
``deepseek-official`` is owned by ``llm-deepseek`` and registry routes
are user-chosen names registered by ``llm-pi-ai``.

Secrets: route credentials are NEVER written into the generated file.
Each configured route names an injected environment variable
(``apiKeyEnv``); the literal key travels to the runtime subprocess via
the environment only (see ``DshSession._default_harness_factory``).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

LLM_PI_AI_PLUGIN_ID = "llm-pi-ai"
LLM_PI_AI_PLUGIN_NAME = "@deepseek-ai/dsh-llm-pi-ai"

USER_APPROVAL_PLUGIN_ID = "user-approval"
USER_APPROVAL_PLUGIN_NAME = "@deepseek-ai/dsh-user-approval"

# The runtime's approval seam (@deepseek-ai/dsh-user-approval) defaults
# to policy "ask" — a prompt no one can answer in a headless SDK session,
# so every bash call that trips the policy is denied outright (observed
# as "Tool call denied: bash reason=policy=ask (not supported in
# autonomous mode)"). Mounting the plugin with ``policy: never`` makes
# the approval service resolve every ask as approved: the headless
# equivalent of the orchestrator's bypassPermissions mode.
APPROVAL_POLICY_NEVER = "never"

# orchestratord permission_mode → runtime approval policy. Only modes
# that explicitly grant autonomy are mapped; anything else keeps the
# runtime's own default ("ask") — fail-closed.
_PERMISSION_MODE_TO_APPROVAL = {
    "bypasspermissions": APPROVAL_POLICY_NEVER,
    # "dontask" historically means deny-all on other backends; the
    # runtime has no such policy, so it is deliberately unmapped.
}

# Wire protocols the runtime's llm-pi-ai adapter can serve
# (supportedProtocols() in the shipped build).
SUPPORTED_APIS = (
    "openai-completions",
    "openai-responses",
    "anthropic-messages",
)

_GENERATED_MARKER = (
    "# --- orchestratord generated: agent.providers routes (llm-pi-ai) ---"
)

# Runtime schema defaults (resolveRouteModels in the shipped build):
# routes may omit these and the runtime fills them in.
_RUNTIME_DEFAULT_CONTEXT_WINDOW = 262144
_RUNTIME_DEFAULT_MAX_TOKENS = 32768


class CordisConfigError(RuntimeError):
    """Raised with an actionable message when a provider route table
    cannot be turned into a runtime configuration."""


def route_env_var_name(route: str) -> str:
    """The environment variable name carrying a route's API key.

    Route names are user-chosen (e.g. ``my-gateway``) and may contain
    characters invalid in POSIX shell identifiers; they are sanitized to
    ``DSH_ROUTE_<NAME>_KEY``. Collisions after sanitization (``a-b`` vs
    ``a_b``) must be caught by :func:`validate_providers` before any
    credential is injected.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", route).upper()
    return f"DSH_ROUTE_{sanitized}_KEY"


def resolve_route_credential(api_key: str | None, environ: dict[str, str] | None = None) -> str | None:
    """Resolve one route's ``api_key`` to a literal secret.

    ``None`` → no credential configured (legitimate: keyless gateways or
    pi-ai's own ambient discovery). A ``$VAR`` reference is resolved from
    the environment; a missing variable raises with the original name so
    the operator can fix the export. Literal values pass through.
    """
    if api_key is None:
        return None
    value = str(api_key).strip()
    if not value:
        return None
    if value.startswith("$"):
        env_name = value[1:].strip("{} ")
        env = os.environ if environ is None else environ
        resolved = env.get(env_name)
        if not resolved:
            raise CordisConfigError(
                f"api_key reference '{value}' cannot be resolved — "
                f"{env_name} is not set in the environment. Export it "
                "or set a literal value / a different reference in "
                "agent.providers."
            )
        return resolved
    return value


def _model_entry(route: str, entry: Any) -> dict[str, Any]:
    """Normalize one configured model to the runtime's camelCase shape.

    Accepts a bare model-id string (shorthand) or a mapping with
    ``id`` plus optional ``name`` / ``context_window`` / ``max_tokens`` /
    ``input``. Omitted contextWindow/maxTokens fall through to the
    runtime's own defaults (262144 / 32768), so a minimal
    ``models: [my-model]`` is valid.
    """
    if isinstance(entry, str):
        model_id = entry.strip()
        extra: dict[str, Any] = {}
    elif isinstance(entry, dict):
        model_id = str(entry.get("id", "")).strip()
        extra = {}
        if entry.get("name"):
            extra["name"] = str(entry["name"])
        for snake, camel in (
            ("context_window", "contextWindow"),
            ("max_tokens", "maxTokens"),
        ):
            value = entry.get(snake)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                extra[camel] = value
        if isinstance(entry.get("input"), list) and entry["input"]:
            extra["input"] = [str(i) for i in entry["input"]]
    else:
        raise CordisConfigError(
            f"provider route '{route}': models entries must be strings or "
            f"mappings, got {type(entry).__name__}"
        )
    if not model_id:
        raise CordisConfigError(
            f"provider route '{route}': a models entry has an empty id"
        )
    return {"id": model_id, **extra}


def _model_id_of(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        return str(entry.get("id", "")).strip()
    return ""


def validate_providers(providers: dict[str, dict[str, Any]]) -> None:
    """Validate a serialized provider route table (as carried in
    ``SessionSpec.extra['providers']``).

    Raises :class:`CordisConfigError` with an actionable message naming
    the offending route. Credential resolution is deliberately NOT done
    here: only the route selected for the current run is resolved (by
    :func:`resolve_route_credential`), so an unresolvable ``$VAR`` on a
    route this run does not use never blocks it.
    """
    if not providers:
        raise CordisConfigError("provider route table is empty")
    seen_env_names: dict[str, str] = {}
    for route, cfg in providers.items():
        if not isinstance(cfg, dict):
            raise CordisConfigError(
                f"provider route '{route}': entry must be a mapping"
            )
        api = cfg.get("api")
        if api is not None and api not in SUPPORTED_APIS:
            raise CordisConfigError(
                f"provider route '{route}': api {api!r} is not served by the "
                f"runtime — supported: {', '.join(SUPPORTED_APIS)}"
            )
        base_url = cfg.get("base_url")
        if base_url is not None and not str(base_url).strip():
            raise CordisConfigError(
                f"provider route '{route}': base_url must not be empty"
            )
        if api is None and not base_url:
            # A route that is neither a pi-ai catalog name nor a
            # repointed endpoint cannot serve anything.
            raise CordisConfigError(
                f"provider route '{route}': needs an api (one of "
                f"{', '.join(SUPPORTED_APIS)}) or a base_url; a hand-declared "
                "gateway must name the wire protocol its endpoint speaks"
            )
        models = cfg.get("models") or []
        model_ids: list[str] = []
        for entry in models:
            model_id = _model_id_of(entry)
            if not model_id:
                raise CordisConfigError(
                    f"provider route '{route}': a models entry has an empty id"
                )
            if model_id in model_ids:
                raise CordisConfigError(
                    f"provider route '{route}': model '{model_id}' is listed "
                    "more than once"
                )
            model_ids.append(model_id)
        for entry in (cfg.get("model_overrides") or {}):
            if not str(entry).strip():
                raise CordisConfigError(
                    f"provider route '{route}': model_overrides has an empty key"
                )
        env_name = route_env_var_name(str(route))
        if env_name in seen_env_names:
            raise CordisConfigError(
                f"provider routes '{seen_env_names[env_name]}' and '{route}' "
                f"both sanitize to the same credential variable "
                f"({env_name}) — rename one of them"
            )
        seen_env_names[env_name] = str(route)


def _route_profile(
    route: str,
    cfg: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Translate one route entry into the llm-pi-ai profile shape.

    ``apiKeyEnv`` is emitted only when the route carries a resolvable
    credential — a keyless route omits it so the runtime falls back to
    pi-ai's own ambient discovery (its documented behavior).
    """
    profile: dict[str, Any] = {}
    if cfg.get("display_name"):
        profile["displayName"] = str(cfg["display_name"])
    if cfg.get("api"):
        profile["api"] = str(cfg["api"])
    if cfg.get("base_url"):
        profile["baseURL"] = str(cfg["base_url"])
    if resolve_route_credential(cfg.get("api_key"), environ) is not None:
        profile["apiKeyEnv"] = route_env_var_name(route)
    models = [_model_entry(route, entry) for entry in (cfg.get("models") or [])]
    if models:
        profile["models"] = models
    overrides = cfg.get("model_overrides") or {}
    if overrides:
        profile["modelOverrides"] = {
            str(name): _model_entry(route, {"id": name, **(body or {})})
            for name, body in overrides.items()
        }
    if cfg.get("headers"):
        profile["headers"] = {str(k): str(v) for k, v in cfg["headers"].items()}
    if cfg.get("default_context_window"):
        profile["defaultContextWindow"] = int(cfg["default_context_window"])
    if cfg.get("default_max_tokens"):
        profile["defaultMaxTokens"] = int(cfg["default_max_tokens"])
    return profile


def permission_mode_to_approval_policy(permission_mode: str | None) -> str | None:
    """Map an orchestratord ``agent.permission_mode`` value to the
    runtime approval-service policy for fresh sessions.

    Only explicitly autonomous modes map to a policy (currently
    ``bypassPermissions`` → ``never`` — the headless bypass). ``None``/
    unknown keeps the runtime default (``ask``) — fail-closed for
    anything the orchestrator does not clearly mark autonomous.
    """
    raw = (permission_mode or "").strip().lower()
    return _PERMISSION_MODE_TO_APPROVAL.get(raw)


def build_approval_block(policy: str) -> str:
    """The YAML text mounting the user-approval seam with an explicit
    ``policy``. Emitted as another top-level list item of the cordis
    config (same text-append strategy as the llm-pi-ai block)."""
    block = [
        {
            "id": USER_APPROVAL_PLUGIN_ID,
            "name": USER_APPROVAL_PLUGIN_NAME,
            "config": {"policy": policy},
        }
    ]
    return yaml.safe_dump(
        block, sort_keys=False, allow_unicode=True, default_flow_style=False
    )


def build_cordis_text(
    providers: dict[str, dict[str, Any]] | None = None,
    *,
    environ: dict[str, str] | None = None,
    approval_policy: str | None = None,
) -> str:
    """Return the full cordis config text: the bundled default (verbatim,
    preserving ``!!js`` tags) plus the appended llm-pi-ai plugin block
    and/or the user-approval policy block.

    The bundled default mounts neither plugin, so text-appending is
    collision-free; a future runtime default that already mounts either
    raises (see ``_bundled_cordis_text``).
    """
    base = _bundled_cordis_text()
    blocks: list[str] = []
    if providers:
        block = [
            {
                "id": LLM_PI_AI_PLUGIN_ID,
                "name": LLM_PI_AI_PLUGIN_NAME,
                "config": {
                    "providers": {
                        route: _route_profile(route, cfg, environ=environ)
                        for route, cfg in providers.items()
                    }
                },
            }
        ]
        blocks.append(
            yaml.safe_dump(
                block, sort_keys=False, allow_unicode=True, default_flow_style=False
            )
        )
    if approval_policy:
        blocks.append(build_approval_block(approval_policy))
    marker_note = (
        f"{_GENERATED_MARKER}\n"
        if providers
        else "# --- orchestratord generated: approval policy ---\n"
    )
    if not blocks:
        raise CordisConfigError(
            "nothing to generate: no provider routes and no approval "
            "policy requested"
        )
    return f"{base.rstrip()}\n\n{marker_note}" + "\n".join(blocks)


def generate_cordis_file(
    providers: dict[str, dict[str, Any]] | None,
    out_dir: Path | str,
    *,
    run_id: str | None = None,
    environ: dict[str, str] | None = None,
    approval_policy: str | None = None,
) -> Path:
    """Write the generated cordis config under ``out_dir`` (the session
    workspace's ``.reports/`` directory) and return its path.

    The filename carries the run id (or a timestamp fallback) so
    concurrent sessions never overwrite each other's config. ``providers``
    and ``approval_policy`` are independent: either alone (or both)
    triggers generation.
    """
    if providers:
        validate_providers(providers)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id or f"t{time.time_ns()}-{uuid.uuid4().hex[:8]}")
    path = directory / f"dsh-cordis-{stem}.yml"
    path.write_text(
        build_cordis_text(providers, environ=environ, approval_policy=approval_policy),
        encoding="utf-8",
    )
    return path


def resolve_route(
    providers: dict[str, dict[str, Any]],
    spec_provider: str | None,
    spec_model: str | None,
    *,
    default_model: str,
) -> tuple[str, str]:
    """Resolve the (provider, model) pair to initialize the runtime with.

    Declaration vs selection semantics:

    * ``spec_provider`` names a route → that route. The literal
      ``deepseek-official`` is allowed even when absent from the table
      (the runtime auto-mounts its stock adapter for that name) — the
      legacy credential chain applies and no cordis is needed.
    * ``spec_provider`` empty → the table must hold exactly one route.
      (``AgentConfig.provider`` defaults to empty since the
      backend-default change; workflows that omit ``agent.provider``
      land here.)
    * ``spec_model`` empty → the route must declare exactly one model.
      A route with no declared models and no explicit selection is an
      error — falling back to the DeepSeek default here would silently
      address a foreign gateway with a DeepSeek model id. (Only the
      deepseek-official fallback path, returned above, uses
      ``default_model``.)
    """
    raw = (spec_provider or "").strip()
    if raw == "deepseek-official" and raw not in providers:
        # Legacy stock-adapter path — caller uses its own credential chain.
        return raw, (spec_model or "").strip() or default_model
    if not raw:
        if len(providers) == 1:
            raw = next(iter(providers))
        elif not providers:
            return raw or "deepseek-official", (spec_model or "").strip() or default_model
        else:
            raise CordisConfigError(
                f"agent.provider is required when multiple provider routes "
                f"are declared ({', '.join(sorted(providers))}) — set it in "
                "the --config file (agent.provider)"
            )
    if raw not in providers:
        raise CordisConfigError(
            f"provider '{raw}' is not declared in agent.providers — "
            f"declared routes: {', '.join(sorted(providers)) or '(none)'}. "
            "Add the route or change agent.provider."
        )
    cfg = providers[raw] or {}
    model = (spec_model or "").strip()
    declared = [
        mid for mid in (_model_id_of(entry) for entry in (cfg.get("models") or []))
        if mid
    ]
    if not model:
        if len(declared) == 1:
            model = declared[0]
        elif len(declared) > 1:
            raise CordisConfigError(
                f"provider route '{raw}' declares multiple models "
                f"({', '.join(declared)}) — set agent.model (or the stage "
                "override) to choose one"
            )
        else:
            # No declared models and no explicit selection. Do NOT fall
            # back to the DeepSeek default: this route is not the stock
            # deepseek-official adapter, and addressing a foreign gateway
            # with a DeepSeek model id would only surface as an opaque
            # provider-side error at turn time.
            raise CordisConfigError(
                f"provider route '{raw}' declares no models and no "
                "agent.model is set — declare models on the route "
                "(agent.providers.<route>.models) or set agent.model"
            )
    elif declared and model not in declared:
        raise CordisConfigError(
            f"model '{model}' is not declared on provider route "
            f"'{raw}' — declared models: {', '.join(declared)}. "
            "Add it to the route's models list or change agent.model."
        )
    return raw, model


# ---------------------------------------------------------------------------
# Bundled default config access + llm-pi-ai availability probe
# ---------------------------------------------------------------------------

_probe_cache: bool | None = False  # False = not yet probed; True/bool result cached


def bundled_default_cordis_text() -> str:
    """The bundled default cordis.yml shipped with the runtime wheel."""
    try:
        from deepseek_harness_runtime import bundled_default_config_path
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise CordisConfigError(
            "deepseek-harness-runtime is not installed — the dsh backend "
            "cannot locate the bundled default cordis config. Install "
            "deepseek-harness-runtime-bin."
        ) from exc
    path = bundled_default_config_path()
    return path.read_text(encoding="utf-8")


def _bundled_cordis_text() -> str:
    text = bundled_default_cordis_text()
    if LLM_PI_AI_PLUGIN_NAME in text:
        # A future runtime default may already mount the adapter; the
        # generated block would then collide with a second mount.
        raise CordisConfigError(
            "the bundled default cordis config already mounts "
            f"{LLM_PI_AI_PLUGIN_NAME}; custom route generation is only "
            "supported for runtimes whose default config does not"
        )
    return text


def probe_llm_pi_ai_available() -> bool:
    """Best-effort check that the runtime build ships the llm-pi-ai plugin.

    Scans the bundled runtime executable for the plugin package name
    (cached per process). Returns False when the bundled runtime is
    missing — callers decide whether that is fatal (a custom
    ``runtime_bin`` makes the probe inconclusive, not negative).
    """
    global _probe_cache
    if _probe_cache is not False:
        return bool(_probe_cache)
    _probe_cache = False
    try:
        from deepseek_harness_runtime import bundled_runtime_path

        exe = Path(bundled_runtime_path())
    except Exception:  # noqa: BLE001 - probe is best-effort by design
        return False
    if not exe.is_file():
        return False
    needle = b"dsh-llm-pi-ai"
    found = False
    try:
        with exe.open("rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(8 * 1024 * 1024)
                if not chunk:
                    break
                if needle in tail + chunk:
                    found = True
                    break
                tail = chunk[-len(needle):]
    except OSError:
        return False
    _probe_cache = found
    return found
