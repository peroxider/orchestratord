"""Workflow configuration schema and validation.

Port of Symphony's Config.Schema (Ecto) to plain Python dataclasses.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..tracker import (
    default_active_states_for_kind,
    default_terminal_states_for_kind,
    normalize_tracker_kind,
    tracker_kind_info,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("$"):
        env_name = value[1:]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", env_name):
            env_value = os.environ.get(env_name)
            if env_value is None or env_value == "":
                return None
            return env_value
    return value


def _normalize_secret_value(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _expand_path(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    resolved = _resolve_env_value(value)
    if resolved is None or resolved == "":
        return fallback
    return os.path.expanduser(resolved)


def _normalize_keys(value: Any, *, _inside_env: bool = False) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k) if _inside_env else str(k).lower()
            # Env var names are case-sensitive; preserve them under any
            # ``env`` key while continuing to normalize all other keys.
            next_inside_env = _inside_env or (not _inside_env and key == "env")
            result[key] = _normalize_keys(v, _inside_env=next_inside_env)
        return result
    if isinstance(value, list):
        return [_normalize_keys(v, _inside_env=_inside_env) for v in value]
    return value


def _drop_nil_values(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            cleaned = _drop_nil_values(v)
            if cleaned is not None:
                result[k] = cleaned
        return result
    if isinstance(value, list):
        return [_drop_nil_values(v) for v in value]
    return value


def _normalize_state_limits(limits: dict[str, Any] | None) -> dict[str, int]:
    if not limits:
        return {}
    result: dict[str, int] = {}
    for state_name, limit in limits.items():
        key = str(state_name).strip().lower()
        if key and isinstance(limit, int) and limit > 0:
            result[key] = limit
    return result


def _normalize_string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(default)


def _normalize_workspace_strategy(value: Any) -> str:
    strategy = str(value or "isolated").strip().lower()
    if strategy not in {"isolated", "shared", "sequential"}:
        raise ValueError("workspace.strategy must be one of: isolated, shared, sequential")
    return strategy


def _parse_providers_config(value: Any) -> dict[str, ProviderConfig]:
    """Build the ``agent.providers`` route table from YAML.

    Tolerant of malformed entries (warn + drop, matching the
    config-loader philosophy): a bad route is surfaced by the consuming
    backend's preflight as an unknown-provider error, not a daemon
    crash. ``api_key`` is kept raw (see :class:`ProviderConfig`).
    """
    if not isinstance(value, dict):
        if value:
            logger.warning("agent.providers must be a mapping; ignoring it")
        return {}
    out: dict[str, ProviderConfig] = {}
    for route_name, route_raw in value.items():
        route = str(route_name).strip()
        if not route:
            logger.warning("agent.providers contains an empty route name; ignored")
            continue
        if not isinstance(route_raw, dict):
            logger.warning(
                "agent.providers[%r] is not a mapping; ignored", route
            )
            continue
        models_raw = route_raw.get("models")
        models: list[Any] = []
        if isinstance(models_raw, list):
            models = list(models_raw)
        elif models_raw not in (None, []):
            logger.warning(
                "agent.providers[%r].models must be a list; ignored", route
            )
        headers_raw = route_raw.get("headers")
        headers: dict[str, str] = {}
        if isinstance(headers_raw, dict):
            headers = {str(k): str(v) for k, v in headers_raw.items() if v is not None}
        elif headers_raw not in (None, {}):
            logger.warning(
                "agent.providers[%r].headers must be a mapping; ignored", route
            )
        overrides_raw = route_raw.get("model_overrides")
        overrides: dict[str, dict[str, Any]] = {}
        if isinstance(overrides_raw, dict):
            overrides = {
                str(k): dict(v) for k, v in overrides_raw.items() if isinstance(v, dict)
            }
        elif overrides_raw not in (None, {}):
            logger.warning(
                "agent.providers[%r].model_overrides must be a mapping; ignored", route
            )
        default_context_window = route_raw.get("default_context_window")
        if not isinstance(default_context_window, int) or isinstance(
            default_context_window, bool
        ) or default_context_window <= 0:
            default_context_window = None
        default_max_tokens = route_raw.get("default_max_tokens")
        if not isinstance(default_max_tokens, int) or isinstance(
            default_max_tokens, bool
        ) or default_max_tokens <= 0:
            default_max_tokens = None
        api = route_raw.get("api")
        out[route] = ProviderConfig(
            api=str(api).strip() if api else None,
            base_url=(str(route_raw.get("base_url")).strip() or None)
            if route_raw.get("base_url")
            else None,
            api_key=(str(route_raw.get("api_key")).strip() or None)
            if route_raw.get("api_key")
            else None,
            models=models,
            model_overrides=overrides,
            headers=headers,
            default_context_window=default_context_window,
            default_max_tokens=default_max_tokens,
            display_name=(str(route_raw.get("display_name")).strip() or None)
            if route_raw.get("display_name")
            else None,
        )
    return out


def _parse_repro_first_config(raw: Any) -> ReproFirstConfig:
    """Build a ``ReproFirstConfig`` from the ``agent.repro_first`` YAML
    section. Tolerant of a missing/malformed section (all defaults,
    gate disabled)."""
    if not isinstance(raw, dict):
        return ReproFirstConfig()

    def _int(key: str, default: int) -> int:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return ReproFirstConfig(
        enabled=bool(raw.get("enabled", False)),
        timeout_ms=_int("timeout_ms", 900_000),
        command_timeout_ms=_int("command_timeout_ms", 300_000),
        labels=_normalize_string_list(raw.get("labels"), default=[]),
    )


def _parse_modes_config(raw: dict[str, Any]) -> ModesConfig:
    """Build a ``ModesConfig`` from the parsed ``modes`` YAML section.

    Tolerant of:
    * missing section (``raw == {}``) → all defaults
    * unknown router kinds → coerced to ``"none"``
    * malformed ``min_confidence`` → coerced to default ``0.5``
    """
    router_raw = raw.get("router") or {}
    pipeline_raw = raw.get("pipeline") or {}
    debate_raw = raw.get("debate") or {}
    swarm_raw = raw.get("swarm") or {}

    router_kind = str(router_raw.get("kind", "none")).strip().lower()
    if router_kind not in {"none", "heuristic", "llm"}:
        logger.warning(
            "modes.router.kind=%r is unknown — falling back to 'none'",
            router_kind,
        )
        router_kind = "none"

    try:
        min_conf = float(router_raw.get("min_confidence", 0.5))
    except (TypeError, ValueError):
        min_conf = 0.5
    min_conf = max(0.0, min(1.0, min_conf))

    try:
        router_timeout = float(router_raw.get("timeout_seconds", 15.0))
    except (TypeError, ValueError):
        router_timeout = 15.0
    router_timeout = max(1.0, router_timeout)

    pipeline_handoff = str(pipeline_raw.get("handoff", "prompt")).strip().lower()
    if pipeline_handoff not in {"prompt", "mailbox"}:
        logger.warning(
            "modes.pipeline.handoff=%r is unknown — falling back to 'prompt'",
            pipeline_handoff,
        )
        pipeline_handoff = "prompt"

    return ModesConfig(
        enabled=_normalize_string_list(raw.get("enabled"), default=["single"]),
        default=str(raw.get("default", "single")).strip().lower() or "single",
        router_kind=router_kind,
        router_model=(
            str(router_raw.get("model", "deepseek-v4-flash")).strip() or "deepseek-v4-flash"
        ),
        router_endpoint=(
            str(router_raw.get("endpoint", "https://api.deepseek.com/chat/completions")).strip()
            or "https://api.deepseek.com/chat/completions"
        ),
        router_api_key_env=(
            str(router_raw.get("api_key_env", "DEEPSEEK_API_KEY")).strip() or "DEEPSEEK_API_KEY"
        ),
        router_timeout_seconds=router_timeout,
        router_min_confidence=min_conf,
        pipeline_stages=_normalize_string_list(
            pipeline_raw.get("stages"),
            default=["analyzer", "implementer", "tester"],
        ),
        pipeline_max_retries_per_stage=max(
            0, int(pipeline_raw.get("max_retries_per_stage", 1) or 0)
        ),
        pipeline_stage_models=_normalize_model_map(pipeline_raw.get("stage_models")),
        pipeline_stage_max_turns=_normalize_int_map(
            pipeline_raw.get("stage_max_turns"), min_value=1
        ),
        pipeline_stage_specs=_normalize_stage_specs(pipeline_raw.get("stage_specs")),
        pipeline_handoff=pipeline_handoff,
        debate_proposers=_normalize_string_list(
            debate_raw.get("proposers"),
            default=["proposer_a", "proposer_b"],
        ),
        debate_judge_model=(
            str(debate_raw["judge_model"]).strip() if debate_raw.get("judge_model") else None
        ),
        debate_proposer_models=_normalize_model_map(debate_raw.get("proposer_models")),
        debate_isolation=_normalize_debate_isolation(debate_raw.get("isolation", "reset")),
        debate_parallel=bool(debate_raw.get("parallel", False)),
        debate_judge_mode=_normalize_debate_judge_mode(debate_raw.get("judge_mode", "pick")),
        swarm_max_subtasks=max(1, int(swarm_raw.get("max_subtasks", 8))),
        swarm_max_parallel=max(1, int(swarm_raw.get("max_parallel", 3))),
        swarm_max_waves=max(1, int(swarm_raw.get("max_waves", 6))),
    )


def _normalize_int_map(value: Any, *, min_value: int = 0) -> dict[str, int]:
    """Same shape as ``_normalize_model_map`` but for int values.

    Silently drops entries whose value can't be coerced to int or is
    below ``min_value``. Useful for per-stage numeric overrides like
    ``max_turns`` where a 0 or negative value is nonsense.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in value.items():
        key = str(k).strip()
        if not key:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv < min_value:
            continue
        out[key] = iv
    return out


def _normalize_stage_specs(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize Pipeline stage_specs YAML into a clean dict.

    Silently drops:
    * non-dict entries
    * entries without a ``kind`` key
    * entries whose kind isn't in the allowed set

    (Silent drop rather than raise because config-loader shouldn't
    crash the daemon on operator typos; PipelineModeRunner will still
    log an unknown-key warning if the referenced stage doesn't exist.)
    """
    if not isinstance(value, dict):
        return {}
    allowed_kinds = {"agent", "debate", "coordinator"}
    out: dict[str, dict[str, Any]] = {}
    for stage_name, spec in value.items():
        if not isinstance(spec, dict):
            logger.warning(
                "modes.pipeline.stage_specs[%r] is not a dict — ignored",
                stage_name,
            )
            continue
        kind = str(spec.get("kind", "agent")).strip().lower()
        if kind not in allowed_kinds:
            logger.warning(
                "modes.pipeline.stage_specs[%r].kind=%r not in %s — ignored",
                stage_name,
                kind,
                sorted(allowed_kinds),
            )
            continue
        config = spec.get("config") or {}
        if not isinstance(config, dict):
            config = {}
        out[str(stage_name).strip()] = {"kind": kind, "config": dict(config)}
    return out


def _normalize_debate_judge_mode(value: Any) -> str:
    candidate = str(value or "pick").strip().lower()
    if candidate not in {"pick", "synthesize"}:
        logger.warning(
            "modes.debate.judge_mode=%r is unknown — falling back to 'pick'",
            candidate,
        )
        return "pick"
    return candidate


def _normalize_model_map(value: Any) -> dict[str, str]:
    """Normalize a YAML map of role-name → model-id into a clean dict.

    Tolerant of:
    * None / missing key → empty dict
    * non-string keys/values → coerced via str() + stripped
    * empty-string values → dropped (signals "use default")
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        key = str(k).strip()
        val = str(v).strip() if v is not None else ""
        if key and val:
            out[key] = val
    return out


def _normalize_debate_isolation(value: Any) -> str:
    candidate = str(value or "reset").strip().lower()
    if candidate not in {"reset", "worktree", "none"}:
        logger.warning(
            "modes.debate.isolation=%r is unknown — falling back to 'reset'",
            candidate,
        )
        return "reset"
    return candidate


def _resolve_orchestrator_permission_mode(
    raw_value: Any,
    *,
    is_orchestrator: bool,
) -> str:
    """Resolve permission_mode with headless auto-override.

    When a workflow.md is being loaded for the orchestrator (detected by the
    presence of a ``tracker`` section), a ``dontAsk`` value — whether explicit
    or default — is auto-promoted to ``bypassPermissions``. This ensures
    fully unattended execution, since ``dontAsk`` may still trigger
    ``ApprovalPolicy`` checks that can block tool calls in headless mode.

    Explicit non-default values are preserved so users can opt back into a
    more restrictive mode if needed.
    """
    raw = str(raw_value).strip() if raw_value else "dontAsk"
    canonical_modes = {
        "acceptedits": "acceptEdits",
        "bypasspermissions": "bypassPermissions",
        "default": "default",
        "dontask": "dontAsk",
        "plan": "plan",
    }
    normalized = canonical_modes.get(raw.lower(), raw)
    if is_orchestrator and normalized == "dontAsk":
        return "bypassPermissions"
    return normalized


_VALID_AUDIT_LOG_LEVELS = {"none", "minimal", "full"}


def _resolve_audit_log(raw_value: Any) -> str:
    """Canonicalize audit_log level."""
    raw = str(raw_value).strip().lower() if raw_value else "minimal"
    if raw in _VALID_AUDIT_LOG_LEVELS:
        return raw
    logger.warning(
        "audit_log=%r is not one of %s; falling back to 'minimal'",
        raw_value,
        sorted(_VALID_AUDIT_LOG_LEVELS),
    )
    return "minimal"


def permission_mode_to_triple(
    permission_mode: str,
    *,
    interactive: bool | None = None,
    default_decision: str | None = None,
    audit_log: str | None = None,
) -> dict[str, Any]:
    """Translate legacy permission_mode enum into three orthogonal fields.

    Explicit overrides take precedence; missing values are inferred from the
    legacy mode. The current wiring only sets ``audit_log``; ``interactive`` and
    ``default_decision`` are reserved for future schema additions.
    """
    mode = str(permission_mode).strip() if permission_mode else "default"
    mapping: dict[str, dict[str, Any]] = {
        "default": {"interactive": True, "default_decision": "ask", "audit_log": "minimal"},
        "plan": {"interactive": True, "default_decision": "ask", "audit_log": "minimal"},
        "acceptEdits": {"interactive": True, "default_decision": "allow", "audit_log": "minimal"},
        "bypassPermissions": {
            "interactive": False,
            "default_decision": "allow",
            "audit_log": "minimal",
        },
        "dontAsk": {"interactive": False, "default_decision": "deny", "audit_log": "minimal"},
        "auto": {"interactive": False, "default_decision": "allow", "audit_log": "minimal"},
        "bubble": {"interactive": True, "default_decision": "ask", "audit_log": "minimal"},
    }
    defaults = mapping.get(mode, mapping["default"])
    result = {
        "interactive": interactive if interactive is not None else defaults["interactive"],
        "default_decision": default_decision
        if default_decision is not None
        else defaults["default_decision"],
        "audit_log": audit_log if audit_log is not None else defaults["audit_log"],
    }
    if result["default_decision"] not in {"allow", "deny", "ask"}:
        result["default_decision"] = defaults["default_decision"]
    return result


def _normalize_title_prefix_match(value: Any) -> str:
    mode = str(value or "any").strip().lower()
    if mode not in {"any", "all"}:
        logger.warning("tracker.title_prefix_match=%r is invalid; using 'any'", value)
        return "any"
    return mode


def _default_tmp_workspace() -> str:
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), "orchestratord_workspaces")


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------


@dataclass
class TrackerConfig:
    kind: str = "linear"
    endpoint: str = "https://api.linear.app/graphql"
    api_key: str | None = None
    # The historical ``cordis`` key was dead — nothing ever read
    # it (the comment claimed DSH_CORDIS_CONFIG forwarding that did not
    # exist). The real chain is agent.cordis → SessionSpec.cordis → SDK.
    project_slug: str | None = None
    owner: str | None = None
    repo: str | None = None
    clone_url: str | None = None
    assignee: str | None = None
    branch_prefix: str | None = None
    issues_path: str | None = None
    active_states: list[str] = field(default_factory=lambda: ["Todo", "In Progress"])
    terminal_states: list[str] = field(
        default_factory=lambda: [
            "Closed",
            "Cancelled",
            "Canceled",
            "Duplicate",
            "Done",
        ]
    )
    # Issues carrying any of these labels (case-insensitive) are
    # excluded from the candidate queue at fetch time. Use for
    # web-only workflow labels (e.g. "completed", "wontfix") that the
    # tracker's `state` field does not reflect as terminal. Empty
    # list = no exclusion.
    skip_labels: list[str] = field(default_factory=list)
    # Issues must carry at least ONE of these labels (OR semantics,
    # case-insensitive) to enter the candidate queue. Use to scope
    # the orchestrator to a particular class of work (e.g. only
    # `priority/high` or `priority/urgent`). Empty list = no
    # requirement. Evaluated before `skip_labels`.
    require_any_labels: list[str] = field(default_factory=list)
    # A candidate title must start with configured prefixes. ``any`` is
    # OR/union semantics and ``all`` is AND/intersection semantics. Empty
    # prefixes disable this filter.
    title_prefixes: list[str] = field(default_factory=list)
    title_prefix_match: str = "any"


@dataclass
class PollingConfig:
    interval_ms: int = 30_000


@dataclass
class WorkspaceConfig:
    root: str = field(default_factory=_default_tmp_workspace)
    hooks: dict[str, Any] = field(default_factory=dict)
    repo_clone_url: str | None = None
    # Fork workflow: upstream repo URL (PR target). Falls back to single-repo
    # mode when absent or equal to repo_clone_url.
    upstream_clone_url: str | None = None
    clone_depth: int | None = 1
    checkout_issue_branch: bool = True
    git_username: str | None = None
    git_email: str | None = None
    git_token: str | None = None
    gitignore_patterns: list[str] = field(default_factory=list)
    strategy: str = "isolated"
    base_branch: str | None = None
    integration_branch: str | None = None
    require_clean_start: bool = True
    require_clean_between_issues: bool = True
    preserve_on_terminal: bool = True
    # Conditional preservation: keep workspace for specific end-states so
    # users can inspect artifacts, re-run verification, or debug failures.
    preserve_on_failure: bool = True
    preserve_on_abandoned: bool = True
    preserve_on_timeout: bool = True
    sequential_lock: bool = True
    # Python interpreter resolution cascade (level 2):
    # workspace-scoped Python interpreter. When ``python_executable``
    # is set, it overrides the ``agent.python_executable`` default.
    # When empty, the resolver will try ``python_auto_detect`` to
    # locate the interpreter from project-level signals
    # (``.python-version``, ``pyvenv.cfg``, ``environment.yml``,
    # ``.venv/pyvenv.cfg``). When detection is disabled or fails,
    # the resolver falls back to ``agent.python_executable`` and
    # finally to "no constraint" (the agent uses PATH ``python3``).
    python_executable: str = ""
    python_auto_detect: bool = True
    # Ordered list of relative paths to probe for python interpreter
    # hints. The first match wins. Default probes cover pyenv, venv,
    # uv/poetry virtualenvs, pipenv, and conda env files.
    python_detect_files: list[str] = field(
        default_factory=lambda: [
            ".python-version",
            "pyvenv.cfg",
            ".venv/pyvenv.cfg",
            "Pipfile",
            "environment.yml",
        ]
    )


@dataclass
class WorkerConfig:
    ssh_hosts: list[str] = field(default_factory=list)
    max_concurrent_agents_per_host: int | None = None


@dataclass
class VerificationConfig:
    timeout_ms: int = 600_000
    # Regression guard (defect R1): when ``agent.test_command`` is empty,
    # verification used to pass vacuously — an agent could break hundreds
    # of existing tests and still ship a "completed" MR. With the guard
    # enabled, git-sync falls back to an auto-detected test run (pytest
    # today) and compares failures against the pre-change baseline; only
    # net-new failures block the push. Repos with no detectable test
    # suite record ``verification_status=skipped_no_tests`` instead of
    # pretending to have passed.
    regression_guard: bool = True
    # Explicit fallback command (overrides auto-detection). Runs from the
    # workspace root; non-zero exit = failing tests.
    fallback_test_command: str = ""


@dataclass
class ReproFirstConfig:
    """Repro-first gate: reproduce the bug before the fix stage may run.

    When enabled, each new issue first gets a reproduction-only agent
    pass that must produce an executable check (non-zero exit while the
    bug exists). Issues whose described behavior cannot be demonstrated
    are failed with a "cannot reproduce" report back on the tracker
    instead of an unverifiable fix MR.
    """

    enabled: bool = False
    # Wall-clock budget for the reproduction agent pass.
    timeout_ms: int = 900_000
    # Budget for executing the reproduction command itself.
    command_timeout_ms: int = 300_000
    # When non-empty, only issues carrying at least one of these labels
    # go through the gate (e.g. ["bug"]); empty means every issue.
    labels: list[str] = field(default_factory=list)


@dataclass
class ProviderConfig:
    """One LLM provider route in ``agent.providers`` (currently consumed
    by the dsh backend, which mounts it as a ``llm-pi-ai`` adapter route
    in the DeepSeek Harness runtime).

    Declaration vs selection: this table declares what the runtime CAN
    serve; ``agent.provider`` / ``agent.model`` (and pipeline
    ``stage_overrides``) merely select among the declared entries and may
    be omitted when the choice is unambiguous (single route / single
    model).

    ``api_key`` is kept RAW (including any ``$VAR`` reference): unlike
    ``agent.api_key`` it is resolved by the consuming backend so an
    unresolvable reference can be reported with the route name and the
    original variable name, and the literal secret never enters the
    generated cordis config (it travels to the runtime subprocess via
    the environment only).

    ``headers`` are STATIC values only: the runtime's llm-pi-ai adapter
    has no CredentialRef support for headers, so ``$VAR`` references are
    rejected at validation and must not be used. Static header values
    are written verbatim into the generated cordis file on disk — never
    place secrets in headers; route credentials belong in ``api_key``
    (the apiKeyEnv pipeline).
    """

    api: str | None = None          # openai-completions | openai-responses | anthropic-messages
    base_url: str | None = None
    api_key: str | None = None      # raw value or $VAR reference (see docstring)
    models: list[Any] = field(default_factory=list)  # str shorthand or {id, name, context_window, max_tokens, input}
    model_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    default_context_window: int | None = None
    default_max_tokens: int | None = None
    display_name: str | None = None


@dataclass
class AgentConfig:
    max_concurrent_agents: int = 10
    max_turns: int = 600
    max_retry_backoff_ms: int = 300_000
    max_retry_attempts: int = 5
    # Base delay (ms) for retries triggered by max_turns being exhausted.
    # Shared retry budget; capped at max_retry_backoff_ms via exponential backoff.
    max_turns_retry_delay_ms: int = 30_000
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)
    # Backend configuration. The selected backend owns provider/runtime
    # semantics; the orchestrator only forwards these generic values.
    # Empty default (historically "anthropic"): each backend applies its
    # own default when unset — dsh falls back to its stock
    # deepseek-official adapter, clawcodex requires an explicit value —
    # so a dsh workflow needs no agent.provider line to be usable.
    provider: str = ""
    permission_mode: str = "dontAsk"
    # Per-tool decision audit log level. "none" disables the NDJSON
    # audit trail; "minimal" records only denied decisions; "full" records
    # every tool call. Defaults to "minimal" to save disk.
    audit_log: str = "minimal"
    test_command: str = ""
    build_command: str = ""
    lint_command: str = ""
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    repro_first: ReproFirstConfig = field(default_factory=ReproFirstConfig)
    # Rate limit on operator-driven retries. When an
    # issue's `IssueRecord.retry_count` reaches this value, the
    # orchestrator refuses to honor further `agent:retry` labels /
    # `/agent retry` comment commands, even with a force flag from
    # the CLI (which is logged as a high-priority audit entry).
    max_retries_per_issue: int = 3
    # Allow `agent:retry` / `agent:follow-up` /
    # `/agent retry` to be triggered by any GitHub-style user, not
    # just the issue author. By default we enforce the strict
    # "author or maintainer only" rule. Setting this to True
    # disables the role check (e.g. for trusted-team scenarios).
    allow_anyone_to_retry: bool = False
    # 429-aware in-turn backoff. When the upstream provider returns
    # HTTP 429 (rate limit) inside a single QueryRunner turn, the
    # AgentRunner sleeps for an exponentially-growing delay and
    # re-issues the same prompt instead of failing immediately. After
    # ``rate_limit_max_retries`` consecutive 429s the circuit breaker
    # opens (``status="rate_limit_circuit_open"``) and the run is
    # handed back to the orchestrator's inter-run retry queue.
    #
    # Model name override. When set, overrides the provider's default
    # model (e.g. ``gpt-4o`` for OpenAI, ``claude-sonnet-4-20250514``
    # for Anthropic).  Leave ``None`` to use the provider's built-in
    # default (which may be a placeholder like ``gpt-5.4`` that does
    # not exist on the real API — see stagnation root-cause analysis).
    model: str | None = None
    # API base URL override (e.g. https://api.minimaxi.com/anthropic for
    # minimax's Anthropic-compatible endpoint).  When None, the backend's
    # default is used.
    base_url: str | None = None
    # API key for the backend provider.  Supports $VAR env-var references
    # (resolved by the workflow loader).  When None, the backend's default
    # credential resolution is used.
    api_key: str | None = None
    # Path to a custom Cordis plugin config YAML for the dsh backend.
    # When set, the dsh runtime loads this instead of its bundled default.
    # The path is forwarded via DSH_CORDIS_CONFIG.
    cordis: str | None = None
    # Path to the dsh runtime binary. When set, the dsh backend uses this
    # instead of the bundled runtime. Useful when the bundled runtime is
    # not available (e.g. placeholder package).
    runtime_bin: str | None = None
    # Multi-model stage overrides: keyed by run_kind (e.g. "review_followup",
    # "agent_followup"), each value is a dict with optional "provider" and/or
    # "model" keys. The orchestrator builds per-stage AgentRunners on top of
    # the main agent config; missing keys inherit from the parent.
    stage_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Named provider routes (declaration, not selection). Currently
    # consumed by the dsh backend, which mounts each entry as a
    # configurable ``llm-pi-ai`` route in the DeepSeek Harness runtime;
    # ``agent.provider`` / ``agent.model`` then select among these
    # routes. Empty dict = the backend default credential chain applies
    # (deepseek-official + DEEPSEEK_API_KEY), fully backward compatible.
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    # the inter-run retry queue between separate AgentRunner.run()
    # invocations; these fields govern backoff WITHIN a single run.
    rate_limit_base_delay_ms: int = 30_000
    rate_limit_max_backoff_ms: int = 600_000
    rate_limit_exponential_factor: float = 2.0
    rate_limit_max_retries: int = 40
    # Minimum interval (ms) between successive provider API requests within
    # a single agent run. When non-zero, the agent sleeps for the remaining
    # time before issuing each new request. Default 1000ms (1s delay) to avoid
    # rate limits on providers with tight per-minute quotas (e.g. MiniMax
    # personal plan). Set to 0 for unlimited request rate.
    delay_between_requests_ms: int = 2000
    run_timeout_ms: int = 1_800_000
    # Stream-stall watchdog: abort a run once the headless
    # session shows no activity (no tool events, no stdout growth) for
    # this long, instead of waiting out the whole run_timeout_ms budget.
    # Default 300s: measured healthy runs pause up to 240s (long LLM
    # turns not streamed to stdout); genuine hangs sat 949s/1140s.
    # 0 disables. See QueryConfig.stall_timeout_s.
    stall_timeout_ms: int = 300_000
    # First-turn timeout in milliseconds.  When set to a positive value,
    # overrides the hardcoded 120s default for ``first_turn_timeout_s``
    # in the ``SessionSpec``.  0 (default) means "not configured" — the
    # runner's built-in 120s fallback applies.  This is the only
    # mechanism to adjust the first-turn deadline for single-turn
    # backends (e.g. opencode) where the entire run is one turn and
    # long bash commands can exceed 120s.
    first_turn_timeout_ms: int = 0
    # Early-diagnosis tier: emit a stall_suspected diagnostic (debug
    # event + WARNING log) after this much silence — guarantees a clear
    # diagnosis within ~30s of a hang without false-kill risk. 0 disables.
    stall_warn_ms: int = 30_000
    # File-path whitelist gate (glob patterns). When non-empty, only files
    # matching at least one pattern may enter the commit.  The gate runs
    # AFTER ``git add -A`` and unstages any file that doesn't match.
    # An empty list disables the gate (default).
    allowed_changed_files: list[str] = field(default_factory=list)
    # Human review gating. When True, the orchestrator marks each
    # completed issue as PENDING_REVIEW instead of COMPLETED after sync,
    # requiring a human to run `orchestrator issue review --id <id> --approve`
    # before the issue transitions to COMPLETED.
    # Works with all tracker kinds (local, GitHub, Gitee, GitCode, Linear).
    review_required: bool = False
    auto_approve: bool = False
    # Multi-agent collaboration preference. The selected backend owns the
    # coordinator implementation and tool set.
    coordinator_mode: bool = False
    # Root-cause fix: stagnation / loop guards. After
    # ``max_no_op_turns`` consecutive turns where the LLM made zero
    # tool calls and produced empty output, the runner emits
    # session_end_reason="stagnation" and breaks the outer while
    # loop. Loop detection: if the same tool-call signature appears
    # ``loop_detection_threshold`` times within the last
    # ``loop_detection_window`` turns, emit
    # session_end_reason="loop_detected".
    max_no_op_turns: int = 3
    loop_detection_window: int = 5
    loop_detection_threshold: int = 3
    # Skip the tracker poll in ``_should_continue`` when the
    # issue state has been identical across ``N`` consecutive polls.
    # Set to 0 to disable the cache and always poll (identical to
    # the pre-cache behaviour). The cache lives on the ``AgentSession``
    # instance — concurrent sessions never share state.
    perf_should_continue_skip_turns: int = 3
    # ProgressSink 协议重构. ``phases`` is the ordered
    # list of named workflow phases the orchestrator drives a session
    # through. When the LLM completes a phase, :class:`ToolContextProgressSink`
    # uses ``(n / total) * 100`` to compute an honest progress
    # percentage; when ``phases`` is empty, the sink reports
    # ``progress=None`` (the dashboard shows "Phase N (进度未知)")
    # instead of the misleading 25/50/75/100 sequence.
    # ``fallback_to_phase_step`` keeps the old ``phase_count * 25``
    # behavior for soft migration periods; new workflows should leave
    # it False and rely on ``phases`` (or explicit LLM ``ProgressReport``
    # calls) for percentage.
    phases: list[str] = field(default_factory=list)
    fallback_to_phase_step: bool = False
    # Root-cause fix: per-turn tool call cap. When the LLM
    # produces more than this many tool calls in a single turn,
    # the agent runner stops processing tool events and waits for
    # SessionComplete to force a turn boundary. This prevents
    # infinite tool-call loops (no SessionComplete emitted) while
    # still allowing complex multi-step operations.
    max_tools_per_turn: int = 50
    # Path of the Python interpreter the agent should use when
    # running shell commands inside the workspace. Empty string
    # (the default) means "do not inject a path instruction; let
    # the LLM rely on PATH." When set, an absolute path here is
    # injected into both the turn-0 issue prompt and the
    # continuation guidance so the agent does not waste turns
    # hunting for the right interpreter. Replace the
    # previously-hardcoded `/root/Conda/bin/python3` in
    # ``PromptBuilder.build_continuation_prompt``.
    python_executable: str = ""
    # Environment variables injected into every Bash subprocess
    # spawned by the agent and every verification/hook subprocess
    # spawned by the orchestrator. Values override inherited daemon
    # env, so ``PATH`` can be extended without breaking the host.
    env: dict[str, str] = field(default_factory=dict)
    # Three-channel clarification flow tuning. These map 1:1 onto
    # ``ClarificationConfig`` fields consumed in orchestrator.py
    # (``getattr(workflow.agent, ...)``). Defaults mirror the module-level
    # ``_DEFAULT_*`` constants in extensions/orchestrator/clarification.py
    # — keep them in sync when retuning.
    clarification_enabled: bool = True
    clarification_timeout_local: float = 30 * 60  # 30 minutes for local channels
    clarification_timeout_author: float = 72 * 3600  # 72 hours for author channel
    max_questions_per_issue: int = 3
    clarification_operator_priority: bool = True  # operator answers beat author
    clarification_simultaneous_grace_ms: float = 5000  # 5 seconds for "tied" answers
    # What to do when all three channels time out: "skip" | "mark_failed" | "notify"
    clarification_escalation: str = "skip"


@dataclass
class SandboxConfig:
    command: str = ""
    approval_policy: str | dict[str, Any] = "never"
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: dict[str, Any] | None = None
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


@dataclass
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    pre_commit: str | None = None
    pre_push: str | None = None
    post_sync: str | None = None
    timeout_ms: int = 60_000


@dataclass
class ReviewFeedbackConfig:
    enabled: bool = False
    mode: str = "manual"
    poll_interval_ms: int = 60_000
    max_feedback_items_per_run: int = 20
    include_ci_failures: bool = True
    reply_to_comments: bool = True
    ignore_authors: list[str] = field(default_factory=list)
    ignored_comment_commands: list[str] = field(default_factory=list)
    ignored_feedback_sources: list[str] = field(default_factory=list)
    ignored_body_patterns: list[str] = field(default_factory=list)
    bot_login: str | None = None
    max_log_chars_per_check: int = 12_000
    max_followup_attempts_per_pr: int = 5
    pending_feedback_timeout_seconds: int = 600


@dataclass
class ObservabilityConfig:
    dashboard_enabled: bool = True
    refresh_ms: int = 1_000
    render_interval_ms: int = 16


@dataclass
class ServerConfig:
    port: int | None = None
    host: str = "127.0.0.1"


@dataclass
class ModesConfig:
    """Multi-agent collaboration-mode configuration.

    Wired by ``orchestrator.Orchestrator`` to instantiate ``ModeSelector``
    plus a ``Router`` backend and register the requested ``ModeRunner``
    implementations. Reading this section in workflow.md is opt-in:
    omitting the section yields ``ModesConfig()`` defaults, which mean
    "Phase-1 behavior — only ``single`` mode is registered and routing
    is disabled".

    YAML shape::

        modes:
          enabled: [single, pipeline]       # which modes to register
          default: single                   # fallback when router fails
          router:
            kind: heuristic                 # heuristic | llm | none
            model: <router-model>           # only used when kind=llm
            min_confidence: 0.5             # router picks below this fall back
          pipeline:
            stages: [analyzer, implementer, tester]

    Unknown keys are ignored — the loader tolerates new keys added in
    later phases so an older daemon can still read a forward-versioned
    workflow.md without crashing.
    """

    enabled: list[str] = field(default_factory=lambda: ["single"])
    default: str = "single"
    router_kind: str = "none"  # "none" | "heuristic" | "llm"
    router_model: str = "deepseek-v4-flash"  # only consulted when router_kind=="llm"
    router_endpoint: str = "https://api.deepseek.com/chat/completions"
    router_api_key_env: str = "DEEPSEEK_API_KEY"
    router_timeout_seconds: float = 15.0
    router_min_confidence: float = 0.5
    pipeline_stages: list[str] = field(
        default_factory=lambda: ["analyzer", "implementer", "tester"]
    )
    # Stage retry: how many times PipelineModeRunner will re-attempt a
    # stage that exited with a terminal-failure status before aborting
    # the whole pipeline. 0 = no retries (legacy behavior).
    pipeline_max_retries_per_stage: int = 1
    # Per-stage model overrides — heterogeneous LLM agents within one
    # pipeline. Each stage name maps to a model id; absent = workflow
    # default. Sequential execution → no concurrent env-var races, so
    # we just try/finally swap workflow.agent.model per stage.
    # Makes Pipeline a *real* multi-agent system (different "agents"
    # via different LLM brains, not just role labels).
    pipeline_stage_models: dict[str, str] = field(default_factory=dict)
    # Per-stage max_turns override — workflow.agent.max_turns is a
    # single value applied everywhere; realistic Pipelines need
    # different budgets per stage (analyzer reads a lot, implementer
    # edits fast, tester runs commands). Absent stage = workflow default.
    pipeline_stage_max_turns: dict[str, int] = field(default_factory=dict)
    # Nested mode dispatch — a Pipeline stage can itself run under a
    # different ModeRunner instead of a plain AgentRunner. Absent /
    # empty = agent (legacy). Only "agent", "debate", "coordinator"
    # are allowed; nested pipeline is rejected to avoid the infinite
    # recursion trap.
    #
    # YAML shape:
    #   modes:
    #     pipeline:
    #       stages: [analyzer, implementer, tester]
    #       stage_specs:
    #         implementer:
    #           kind: debate
    #           config:
    #             proposers: [conservative, bold]
    #             judge_mode: synthesize
    #             isolation: worktree
    pipeline_stage_specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Handoff strategy between Pipeline stages:
    #   "prompt"  — inject prior output as text in next stage's prompt (legacy)
    #   "mailbox" — each stage SendMessage(to=<next stage>); next stage Reads
    #               its mailbox first. Uses the existing team.json /
    #               SendMessage infra from the Coordinator mode work.
    pipeline_handoff: str = "prompt"
    debate_proposers: list[str] = field(default_factory=lambda: ["proposer_a", "proposer_b"])
    # Optional stronger model for the judge stage. None = use the
    # workflow's default agent.model (same as proposers). Set to e.g.
    # "deepseek-v4" to upgrade just the judging step.
    debate_judge_model: str | None = None
    # Judge behavior:
    #   "pick"       — pick 1 winning proposer verbatim (default; legacy)
    #   "synthesize" — combine best ideas from ALL proposers into a
    #                  hybrid solution, citing which proposer contributed
    #                  each piece. Better fit when both proposals have
    #                  genuine merits and you don't have to pick one.
    debate_judge_mode: str = "pick"
    # Per-proposer model overrides — only honored in sequential mode.
    # In parallel mode (see debate_parallel) all proposers share the
    # workflow default model to avoid concurrent env mutations.
    debate_proposer_models: dict[str, str] = field(default_factory=dict)
    # Workspace isolation strategy between proposers (and before judge):
    #   "reset"    — git reset --hard + git clean (default; cheap, single dir)
    #   "worktree" — git worktree add per proposer (real physical isolation)
    #   "none"     — no isolation (proposer A's edits leak to proposer B)
    debate_isolation: str = "reset"
    # Parallel proposers (asyncio.gather). Requires isolation=worktree
    # so each parallel branch has its own physical workspace. When False
    # (default), proposers run sequentially.
    debate_parallel: bool = False
    # Dynamic task decomposition. The seed task graph is persisted in
    # the issue workspace and executed through the existing coordinator mode.
    swarm_max_subtasks: int = 8
    swarm_max_parallel: int = 3
    swarm_max_waves: int = 6


# ---------------------------------------------------------------------------
# Top-level WorkflowConfig
# ---------------------------------------------------------------------------


@dataclass
class RulesConfig:
    """Configuration for learned rule extraction from PR review feedback."""

    enabled: bool = False
    path: str = ""
    max_rules: int = 20
    min_confidence: str = "low"


@dataclass
class TelemetryConfig:
    """Remote telemetry reporting config (daily run summary -> GitCode issue).

    Local event recording is always on (~/.orchestratord/telemetry/events/);
    this only controls the optional remote daily report. ``api_key`` left
    empty reuses the tracker's GitCode token from the workflow.
    """

    reporting_enabled: bool = False
    report_owner: str = ""
    report_repo: str = ""
    issue_title: str = "Orchestratord Telemetry"
    api_key: str = ""
    env_label: str = ""


@dataclass
class PrConflictScanConfig:
    """Configuration for the optional PR conflict scan daemon job.

    When ``enabled=False`` (the default) the daemon does not poll the
    remote PR mergeable state at all — operators must trigger rebase
    via CLI / label / comment. Setting ``enabled=True`` turns on a
    background scan that, for each open PR with a workspace + branch,
    asks the tracker for the mergeable state and invokes
    ``rebase_for_pr`` when ``has_conflicts`` is True.

    Why this is opt-in: GitCode does not reliably expose ``mergeable``
    (JS-rendered page), so the scan is a no-op there. Operators on
    GitHub / Gitee can opt-in for proactive conflict detection; on
    GitCode the other three triggers remain the canonical path.
    """

    enabled: bool = False
    poll_interval_ms: int = 300_000  # 5 minutes
    max_rebase_attempts_per_issue: int = 3
    max_prs_per_scan: int = 25
    use_force_push: bool = False  # corresponds to CLI --force
    bot_login: str | None = None
    scan_states: tuple[str, ...] = ("open",)


@dataclass
class ClarifierConfig:
    """Pre-dispatch issue clarity analysis.

    This config is deliberately separate from ``ClarificationConfig`` in
    ``clarification.py``. The clarifier decides *whether* a question is
    needed; the existing resolver owns delivery, replies, and escalation.
    """

    enabled: bool = False
    block_on_unclear: bool = True
    author_first: bool = True
    max_questions: int = 3
    max_rounds: int = 2
    min_confidence: float = 0.7
    max_input_tokens: int = 6000
    max_output_tokens: int = 800
    fail_open: bool = True
    cache_enabled: bool = True
    max_analyses_per_poll: int = 4
    # Follow-up workspace focus 富化
    workspace_focus_enabled: bool = False
    # 运营增强 2: 可选的专用远端等待标签，空字符串=不推送
    remote_label: str = ""


@dataclass
class PrTemplateConfig:
    """Optional, workflow-defined pull request title and body templates.

    Templates are rendered by :class:`GitSyncService` with a deliberately
    small, data-only set of ``{{ variable }}`` placeholders.  An empty body
    preserves the built-in PR body for backwards compatibility.
    """

    title: str = ""
    body: str = ""


# ---------------------------------------------------------------------------
# P5 配置分段（DESIGN §4.6 B5/:334-336、§6 :432、P5 :471）
# ---------------------------------------------------------------------------


_KERNEL_SECTION_KEYS = frozenset(
    {"polling", "worker", "sandbox", "modes", "observability", "server"}
)
_APPLICATION_SECTION_KEYS = frozenset(
    {
        "tracker",
        "workspace",
        "agent",
        "hooks",
        "review_feedback",
        "rules",
        "telemetry",
        "pr_template",
        "pr_conflict_scan",
        "clarifier",
    }
)


def _lift_dual_format_sections(raw: dict[str, Any]) -> dict[str, Any]:
    """把 ``kernel:`` / ``applications:`` 双格式平移回既有扁平段布局。

    无 ``applications:`` 段 → 旧扁平格式：打 deprecation 日志后原样返回
    （DESIGN :336）。有则按新格式处理：``kernel:`` 与
    ``applications.issue_pr:`` 下的机制/业务段平移回顶层键，交由既有
    扁平解析路径处理（单一解析实现，两种布局零解析差）。同名段同时
    出现在新旧位置时新格式优先（warn）。未知应用段/未知子段按 loader
    宽容哲学忽略（同 ModesConfig）。
    """
    applications_raw = raw.get("applications")
    if applications_raw is None:
        logger.warning(
            "workflow config: no `applications:` section — loaded via the "
            "legacy flat layout (deprecated); see DESIGN §4.6 for the "
            "kernel:/applications: dual format"
        )
        return raw
    if not isinstance(applications_raw, dict):
        raise TypeError("applications: must be a mapping of application name → settings")
    kernel_raw = raw.get("kernel") or {}
    if not isinstance(kernel_raw, dict):
        raise TypeError("kernel: must be a mapping")
    issue_pr_raw = applications_raw.get("issue_pr") or {}
    if not isinstance(issue_pr_raw, dict):
        raise TypeError("applications.issue_pr: must be a mapping")

    lifted: dict[str, tuple[Any, str]] = {}
    for section, value in kernel_raw.items():
        if section in _KERNEL_SECTION_KEYS:
            lifted[section] = (value, "kernel")
    for section, value in issue_pr_raw.items():
        if section in _APPLICATION_SECTION_KEYS:
            lifted[section] = (value, "applications.issue_pr")

    flat = {
        key: value for key, value in raw.items() if key not in ("kernel", "applications")
    }
    for section, (value, source_key) in lifted.items():
        if section in flat:
            logger.warning(
                "workflow config: section `%s` present both flat and under "
                "`%s:` — the dual-format location wins",
                section,
                source_key,
            )
        flat[section] = value
    return flat


@dataclass
class KernelSettings:
    """机制域设置视图（B5：kernel 只读本段，业务段由 Application 解析）。

    组合壳：字段是对 :class:`WorkflowConfig` 既有嵌套段的**引用**——
    不复制、不散射字段，保证零行为差且随宿主同步。``agent`` 为机制
    读面（并发/超时/provider/stage_overrides 等）；业务读面
    （repro_first/max_retries_per_issue/clarification_* 等）经
    :class:`IssuePrSettings`。两视图的 ``agent`` 是同一实例。

    偏差注记：DESIGN :335 的 AgentConfig 字段级上/下移与 :321 的
    kernel 级 ``max_concurrent`` 标量未在本步实施——会打散 128 处
    ``workflow.agent.*`` 直读面；随 Kernel dispatch-loop 抽取收敛。
    """

    polling: PollingConfig
    worker: WorkerConfig
    sandbox: SandboxConfig
    modes: ModesConfig
    observability: ObservabilityConfig
    server: ServerConfig
    agent: AgentConfig


@dataclass
class IssuePrSettings:
    """issue→PR 业务设置视图（Application 自己解析业务段，B5）。

    组合壳同 :class:`KernelSettings`：字段为嵌套段引用；
    ``agent`` 为业务读面（与 KernelSettings.agent 同一实例）。
    """

    tracker: TrackerConfig
    workspace: WorkspaceConfig
    agent: AgentConfig
    hooks: HooksConfig
    review_feedback: ReviewFeedbackConfig
    rules: RulesConfig
    telemetry: TelemetryConfig
    pr_template: PrTemplateConfig
    pr_conflict_scan: PrConflictScanConfig
    clarifier: ClarifierConfig


@dataclass
class WorkflowConfig:
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    review_feedback: ReviewFeedbackConfig = field(default_factory=ReviewFeedbackConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    modes: ModesConfig = field(default_factory=ModesConfig)
    pr_template: PrTemplateConfig = field(default_factory=PrTemplateConfig)
    pr_conflict_scan: PrConflictScanConfig = field(default_factory=lambda: PrConflictScanConfig())
    clarifier: ClarifierConfig = field(default_factory=lambda: ClarifierConfig())
    source_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkflowConfig:
        """Build from a raw dict (already parsed YAML front matter)."""
        raw = _normalize_keys(_drop_nil_values(raw))
        # P5 双格式（DESIGN §4.6/:336）：kernel:/applications: 平移回扁平
        # 布局；旧扁平格式打 deprecation 日志后原样通过。
        raw = _lift_dual_format_sections(raw)

        tracker_raw = raw.get("tracker", {})
        polling_raw = raw.get("polling", {})
        workspace_raw = raw.get("workspace", {})
        worker_raw = raw.get("worker", {})
        agent_raw = raw.get("agent", {})
        codex_raw = raw.get("sandbox") or raw.get("codex") or {}
        hooks_raw = raw.get("hooks", {})
        review_feedback_raw = raw.get("review_feedback", {})
        rules_raw = raw.get("rules", {})
        telemetry_raw = raw.get("telemetry", {})
        modes_raw = raw.get("modes", {}) or {}
        observability_raw = raw.get("observability", {})
        server_raw = raw.get("server", {})
        pr_conflict_scan_raw = raw.get("pr_conflict_scan", {})
        clarifier_raw = raw.get("clarifier", {}) or {}
        pr_template_raw = raw.get("pr_template", {}) or {}
        if not isinstance(pr_template_raw, dict):
            logger.warning("pr_template must be a mapping; ignoring it")
            pr_template_raw = {}

        tracker_kind = normalize_tracker_kind(tracker_raw.get("kind", "linear"))
        tracker_info = tracker_kind_info(tracker_kind)
        tracker_active_states = _normalize_string_list(
            tracker_raw.get("active_states"),
            default_active_states_for_kind(tracker_kind),
        )
        tracker_terminal_states = _normalize_string_list(
            tracker_raw.get("terminal_states"),
            default_terminal_states_for_kind(tracker_kind),
        )
        tracker_skip_labels = _normalize_string_list(
            tracker_raw.get("skip_labels"),
            [],
        )
        tracker_require_any_labels = _normalize_string_list(
            tracker_raw.get("require_any_labels"),
            [],
        )
        tracker_title_prefixes = _normalize_string_list(tracker_raw.get("title_prefixes"), [])
        tracker_title_prefix_match = _normalize_title_prefix_match(
            tracker_raw.get(
                "title_prefix_match",
                tracker_raw.get("title_prefix_match_mode", tracker_raw.get("title_prefix_mode")),
            )
        )

        tracker = TrackerConfig(
            kind=tracker_kind,
            endpoint=_resolve_env_value(tracker_raw.get("endpoint"))
            or tracker_info.default_endpoint,
            api_key=_normalize_secret_value(_resolve_env_value(tracker_raw.get("api_key")))
            or _resolve_first_env(tracker_info.api_key_env_vars),
            project_slug=tracker_raw.get("project_slug"),
            owner=_resolve_env_value(tracker_raw.get("owner"))
            or _resolve_first_env(tracker_info.owner_env_vars),
            repo=_resolve_env_value(tracker_raw.get("repo"))
            or _resolve_first_env(tracker_info.repo_env_vars),
            clone_url=_resolve_env_value(tracker_raw.get("clone_url")),
            assignee=_resolve_env_value(tracker_raw.get("assignee"))
            or _resolve_first_env(tracker_info.assignee_env_vars),
            branch_prefix=_resolve_env_value(tracker_raw.get("branch_prefix")),
            issues_path=_normalize_secret_value(_expand_path(tracker_raw.get("issues_path"), "")),
            active_states=tracker_active_states,
            terminal_states=tracker_terminal_states,
            skip_labels=tracker_skip_labels,
            require_any_labels=tracker_require_any_labels,
            title_prefixes=tracker_title_prefixes,
            title_prefix_match=tracker_title_prefix_match,
        )

        workspace_root = _expand_path(workspace_raw.get("root"), _default_tmp_workspace())
        workspace_strategy = _normalize_workspace_strategy(workspace_raw.get("strategy"))
        workspace = WorkspaceConfig(
            root=workspace_root,
            hooks=workspace_raw.get("hooks", {}),
            repo_clone_url=_resolve_env_value(workspace_raw.get("repo_clone_url")),
            upstream_clone_url=_resolve_env_value(workspace_raw.get("upstream_clone_url")),
            clone_depth=workspace_raw.get("clone_depth", 1),
            checkout_issue_branch=workspace_raw.get("checkout_issue_branch", True),
            git_username=_resolve_env_value(workspace_raw.get("git_username")),
            git_email=_resolve_env_value(workspace_raw.get("git_email")),
            git_token=_normalize_secret_value(_resolve_env_value(workspace_raw.get("git_token"))),
            gitignore_patterns=_normalize_string_list(
                workspace_raw.get("gitignore_patterns"),
                default=[
                    ".orchestrator_control",
                    ".operator_hints.md",
                    ".reports",
                    # Agent-internal session persistence (dsh SDK
                    # transcripts) — never part of an implementation
                    # commit (see git/sync.py for the sync-side list).
                    ".sessions",
                    "*.pyc",
                    "__pycache__",
                    "*.egg-info",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                    "*.log",
                ],
            ),
            strategy=workspace_strategy,
            base_branch=_resolve_env_value(workspace_raw.get("base_branch")),
            integration_branch=_resolve_env_value(workspace_raw.get("integration_branch")),
            require_clean_start=bool(workspace_raw.get("require_clean_start", True)),
            require_clean_between_issues=bool(
                workspace_raw.get("require_clean_between_issues", True)
            ),
            preserve_on_terminal=bool(workspace_raw.get("preserve_on_terminal", True)),
            preserve_on_failure=bool(workspace_raw.get("preserve_on_failure", True)),
            preserve_on_abandoned=bool(workspace_raw.get("preserve_on_abandoned", True)),
            preserve_on_timeout=bool(workspace_raw.get("preserve_on_timeout", True)),
            sequential_lock=bool(workspace_raw.get("sequential_lock", True)),
        )

        verification_raw = agent_raw.get("verification", {})
        # Multi-model stage overrides: parse agent.stages YAML dict.
        stages_raw = agent_raw.get("stages", {}) or {}
        stage_overrides: dict[str, dict[str, Any]] = {}
        for stage_name, stage_cfg in stages_raw.items():
            if not isinstance(stage_cfg, dict):
                continue
            override: dict[str, Any] = {}
            provider = _resolve_env_value(stage_cfg.get("provider"))
            model = _resolve_env_value(stage_cfg.get("model"))
            if provider:
                override["provider"] = provider
            if model:
                override["model"] = model
            if override:
                stage_overrides[stage_name] = override
        # Named provider routes (declaration table; consumed by the dsh
        # backend — see ProviderConfig).
        providers = _parse_providers_config(agent_raw.get("providers"))
        agent = AgentConfig(
            max_concurrent_agents=agent_raw.get("max_concurrent_agents", 10),
            max_turns=agent_raw.get("max_turns", 600),
            max_retry_backoff_ms=agent_raw.get("max_retry_backoff_ms", 300_000),
            max_retry_attempts=agent_raw.get("max_retry_attempts", 5),
            max_turns_retry_delay_ms=agent_raw.get("max_turns_retry_delay_ms", 30_000),
            max_concurrent_agents_by_state=_normalize_state_limits(
                agent_raw.get("max_concurrent_agents_by_state")
            ),
            provider=str(agent_raw.get("provider", "") or "").strip(),
            permission_mode=_resolve_orchestrator_permission_mode(
                agent_raw.get("permission_mode"),
                is_orchestrator=bool(tracker_raw),
            ),
            # Orthogonal audit_log level, independent of permission_mode.
            audit_log=_resolve_audit_log(agent_raw.get("audit_log")),
            test_command=_resolve_env_value(agent_raw.get("test_command")) or "",
            build_command=_resolve_env_value(agent_raw.get("build_command")) or "",
            lint_command=_resolve_env_value(agent_raw.get("lint_command")) or "",
            verification=VerificationConfig(
                timeout_ms=verification_raw.get("timeout_ms", 600_000),
                regression_guard=bool(verification_raw.get("regression_guard", True)),
                fallback_test_command=(
                    _resolve_env_value(verification_raw.get("fallback_test_command")) or ""
                ),
            ),
            repro_first=_parse_repro_first_config(agent_raw.get("repro_first") or {}),
            # Retry rate limit + role check settings
            max_retries_per_issue=agent_raw.get("max_retries_per_issue", 3),
            allow_anyone_to_retry=bool(agent_raw.get("allow_anyone_to_retry", False)),
            # 429-aware in-turn backoff (see AgentConfig docstring above)
            rate_limit_base_delay_ms=agent_raw.get("rate_limit_base_delay_ms", 30_000),
            rate_limit_max_backoff_ms=agent_raw.get("rate_limit_max_backoff_ms", 600_000),
            rate_limit_exponential_factor=float(
                agent_raw.get("rate_limit_exponential_factor", 2.0)
            ),
            rate_limit_max_retries=agent_raw.get("rate_limit_max_retries", 40),
            delay_between_requests_ms=agent_raw.get("delay_between_requests_ms", 2000),
            run_timeout_ms=agent_raw.get("run_timeout_ms", 1_800_000),
            stall_timeout_ms=agent_raw.get("stall_timeout_ms", 300_000),
            first_turn_timeout_ms=agent_raw.get("first_turn_timeout_ms", 0),
            stall_warn_ms=agent_raw.get("stall_warn_ms", 30_000),
            # File-path whitelist gate (see AgentConfig docstring).
            allowed_changed_files=_normalize_string_list(
                agent_raw.get("allowed_changed_files"), default=[]
            ),
            # Review gate — when True, sync ends at PENDING_REVIEW
            # instead of COMPLETED, requiring human approve CLI command.
            review_required=bool(agent_raw.get("review_required", False)),
            auto_approve=bool(agent_raw.get("auto_approve", False)),
            # MVP multi-agent: coordinator mode toggle (from workflow.md)
            coordinator_mode=bool(agent_raw.get("coordinator_mode", False)),
            # Named workflow phases drive honest progress
            # percentages in ToolContextProgressSink. ``phases`` is
            # parsed as a list (the YAML ``- a`` / ``- b`` syntax)
            # and defaults to empty. ``fallback_to_phase_step``
            # reverts to the legacy ``phase_count * 25`` step function
            # without crashing the loader. ``fallback_to_phase_step``
            # defaults to ``False`` so new workflows see ``None``
            # instead of misleading 25/50/75/100.
            phases=_normalize_string_list(agent_raw.get("phases"), default=[]),
            fallback_to_phase_step=bool(agent_raw.get("fallback_to_phase_step", False)),
            # Root-cause fix: stagnation / loop guard knobs.
            # These were defined in AgentConfig (schema.py) and set in
            # workflow.md, but ``from_dict`` never forwarded them to the
            # dataclass constructor, so the schema defaults (3/5/3) were
            # always used regardless of the YAML config.
            max_no_op_turns=int(agent_raw.get("max_no_op_turns", 3)),
            loop_detection_window=int(agent_raw.get("loop_detection_window", 5)),
            loop_detection_threshold=int(agent_raw.get("loop_detection_threshold", 3)),
            # Per-turn tool cap: schema default was 50 but ``from_dict`` did not
            # forward the YAML value, so workflow.md edits were ignored.
            max_tools_per_turn=int(agent_raw.get("max_tools_per_turn", 50)),
            # Root-cause fix: model name override.
            model=_resolve_env_value(agent_raw.get("model")) or None,
            base_url=_resolve_env_value(agent_raw.get("base_url")) or None,
            api_key=_resolve_env_value(agent_raw.get("api_key")) or None,
            cordis=_resolve_env_value(agent_raw.get("cordis")) or None,
            runtime_bin=_resolve_env_value(agent_raw.get("runtime_bin")) or None,
            # Python interpreter hint injected into agent prompts (see
            # AgentConfig.python_executable). Historically parsed from
            # YAML was silently dropped here — the field existed but
            # from_dict never forwarded it.
            python_executable=str(agent_raw.get("python_executable", "") or "").strip(),
            # Multi-model stage overrides (parsed above).
            stage_overrides=stage_overrides,
            # Named provider routes (parsed above).
            providers=providers,
            # Per-run env vars merged into Bash/hook subprocess env.
            env={str(k): str(v) for k, v in (agent_raw.get("env") or {}).items() if v is not None},
            # Three-channel clarification flow tuning. Keys mirror the
            # ``getattr(workflow.agent, ...)`` reads in orchestrator.py;
            # defaults mirror the ``_DEFAULT_*`` constants in clarification.py.
            clarification_enabled=bool(agent_raw.get("clarification_enabled", True)),
            clarification_timeout_local=float(
                agent_raw.get("clarification_timeout_local", 30 * 60)
            ),
            clarification_timeout_author=float(
                agent_raw.get("clarification_timeout_author", 72 * 3600)
            ),
            max_questions_per_issue=int(agent_raw.get("max_questions_per_issue", 3)),
            clarification_operator_priority=bool(
                agent_raw.get("clarification_operator_priority", True)
            ),
            clarification_simultaneous_grace_ms=float(
                agent_raw.get("clarification_simultaneous_grace_ms", 5000)
            ),
            clarification_escalation=str(agent_raw.get("clarification_escalation", "skip")),
        )
        if workspace.strategy == "sequential":
            if agent.max_concurrent_agents != 1:
                raise ValueError(
                    "workspace.strategy=sequential requires agent.max_concurrent_agents=1"
                )
            over_limit_states = [
                state for state, limit in agent.max_concurrent_agents_by_state.items() if limit > 1
            ]
            if over_limit_states:
                raise ValueError(
                    "workspace.strategy=sequential requires all "
                    "agent.max_concurrent_agents_by_state values to be <= 1"
                )

        sandbox = SandboxConfig(
            command=codex_raw.get("command", ""),
            approval_policy=codex_raw.get("approval_policy", SandboxConfig().approval_policy),
            thread_sandbox=codex_raw.get("thread_sandbox", "workspace-write"),
            turn_sandbox_policy=codex_raw.get("turn_sandbox_policy"),
            turn_timeout_ms=codex_raw.get("turn_timeout_ms", 3_600_000),
            read_timeout_ms=codex_raw.get("read_timeout_ms", 5_000),
            stall_timeout_ms=codex_raw.get("stall_timeout_ms", 300_000),
        )

        hooks = HooksConfig(
            after_create=_resolve_env_value(hooks_raw.get("after_create")),
            before_run=_resolve_env_value(hooks_raw.get("before_run")),
            after_run=_resolve_env_value(hooks_raw.get("after_run")),
            before_remove=_resolve_env_value(hooks_raw.get("before_remove")),
            pre_commit=_resolve_env_value(hooks_raw.get("pre_commit")),
            pre_push=_resolve_env_value(hooks_raw.get("pre_push")),
            post_sync=_resolve_env_value(hooks_raw.get("post_sync")),
            timeout_ms=hooks_raw.get("timeout_ms", 60_000),
        )

        return cls(
            tracker=tracker,
            polling=PollingConfig(interval_ms=polling_raw.get("interval_ms", 30_000)),
            workspace=workspace,
            worker=WorkerConfig(
                ssh_hosts=worker_raw.get("ssh_hosts", []),
                max_concurrent_agents_per_host=worker_raw.get("max_concurrent_agents_per_host"),
            ),
            agent=agent,
            sandbox=sandbox,
            hooks=hooks,
            rules=RulesConfig(
                enabled=bool(rules_raw.get("enabled", False)),
                path=str(rules_raw.get("path", "")).strip(),
                max_rules=int(rules_raw.get("max_rules", 20)),
                min_confidence=str(rules_raw.get("min_confidence", "low")).strip().lower(),
            ),
            telemetry=TelemetryConfig(
                reporting_enabled=bool(telemetry_raw.get("reporting_enabled", False)),
                report_owner=str(telemetry_raw.get("report_owner", "")).strip(),
                report_repo=str(telemetry_raw.get("report_repo", "")).strip(),
                issue_title=str(
                    telemetry_raw.get("issue_title", "Orchestratord Telemetry")
                ).strip(),
                api_key=str(telemetry_raw.get("api_key", "")).strip(),
                env_label=str(telemetry_raw.get("env_label", "")).strip(),
            ),
            review_feedback=ReviewFeedbackConfig(
                enabled=bool(review_feedback_raw.get("enabled", False)),
                mode=str(review_feedback_raw.get("mode", "manual")).strip().lower() or "manual",
                poll_interval_ms=review_feedback_raw.get("poll_interval_ms", 60_000),
                max_feedback_items_per_run=review_feedback_raw.get(
                    "max_feedback_items_per_run", 20
                ),
                include_ci_failures=bool(review_feedback_raw.get("include_ci_failures", True)),
                reply_to_comments=bool(review_feedback_raw.get("reply_to_comments", True)),
                ignore_authors=_normalize_string_list(
                    review_feedback_raw.get("ignore_authors"), []
                ),
                ignored_comment_commands=_normalize_string_list(
                    review_feedback_raw.get("ignored_comment_commands"), []
                ),
                ignored_feedback_sources=_normalize_string_list(
                    review_feedback_raw.get("ignored_feedback_sources"), []
                ),
                ignored_body_patterns=_normalize_string_list(
                    review_feedback_raw.get("ignored_body_patterns"), []
                ),
                bot_login=_resolve_env_value(review_feedback_raw.get("bot_login")),
                max_log_chars_per_check=review_feedback_raw.get("max_log_chars_per_check", 12_000),
                max_followup_attempts_per_pr=review_feedback_raw.get(
                    "max_followup_attempts_per_pr", 5
                ),
                pending_feedback_timeout_seconds=review_feedback_raw.get(
                    "pending_feedback_timeout_seconds", 600
                ),
            ),
            observability=ObservabilityConfig(
                dashboard_enabled=observability_raw.get("dashboard_enabled", True),
                refresh_ms=observability_raw.get("refresh_ms", 1_000),
                render_interval_ms=observability_raw.get("render_interval_ms", 16),
            ),
            server=ServerConfig(
                port=server_raw.get("port"),
                host=server_raw.get("host", "127.0.0.1"),
            ),
            modes=_parse_modes_config(modes_raw),
            pr_template=PrTemplateConfig(
                title=str(pr_template_raw.get("title", "") or "").strip(),
                body=str(pr_template_raw.get("body", "") or ""),
            ),
            pr_conflict_scan=PrConflictScanConfig(
                enabled=bool(pr_conflict_scan_raw.get("enabled", False)),
                poll_interval_ms=pr_conflict_scan_raw.get("poll_interval_ms", 300_000),
                max_rebase_attempts_per_issue=pr_conflict_scan_raw.get(
                    "max_rebase_attempts_per_issue", 3
                ),
                max_prs_per_scan=pr_conflict_scan_raw.get("max_prs_per_scan", 25),
                use_force_push=bool(pr_conflict_scan_raw.get("use_force_push", False)),
                bot_login=_resolve_env_value(pr_conflict_scan_raw.get("bot_login")),
                scan_states=tuple(
                    _normalize_string_list(pr_conflict_scan_raw.get("scan_states"), ["open"])
                ),
            ),
            clarifier=ClarifierConfig(
                enabled=bool(clarifier_raw.get("enabled", False)),
                block_on_unclear=bool(clarifier_raw.get("block_on_unclear", True)),
                author_first=bool(clarifier_raw.get("author_first", True)),
                max_questions=max(1, int(clarifier_raw.get("max_questions", 3))),
                max_rounds=max(1, int(clarifier_raw.get("max_rounds", 2))),
                min_confidence=max(
                    0.0,
                    min(1.0, float(clarifier_raw.get("min_confidence", 0.7))),
                ),
                max_input_tokens=max(256, int(clarifier_raw.get("max_input_tokens", 6000))),
                max_output_tokens=max(128, int(clarifier_raw.get("max_output_tokens", 800))),
                fail_open=bool(clarifier_raw.get("fail_open", True)),
                cache_enabled=bool(clarifier_raw.get("cache_enabled", True)),
                max_analyses_per_poll=max(
                    1,
                    int(clarifier_raw.get("max_analyses_per_poll", 4)),
                ),
            ),
        )

    def resolve_turn_sandbox_policy(self, workspace_path: str | None = None) -> dict[str, Any]:
        if self.sandbox.turn_sandbox_policy:
            return self.sandbox.turn_sandbox_policy
        root = workspace_path or self.workspace.root
        return {
            "type": "workspaceWrite",
            "writableRoots": [root],
            "readOnlyAccess": {"type": "fullAccess"},
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }

    @property
    def kernel(self) -> KernelSettings:
        """机制域设置视图（DESIGN §4.6 B5/:334；P5）。

        每次访问重建壳——嵌套段可能被宿主事后替换（如
        ``backend_runner.run_task`` 对 ``workflow.agent`` 的拷贝改写），
        重建保证视图不滞留旧段；壳字段是对既有段的引用，零复制。
        """
        return KernelSettings(
            polling=self.polling,
            worker=self.worker,
            sandbox=self.sandbox,
            modes=self.modes,
            observability=self.observability,
            server=self.server,
            agent=self.agent,
        )

    @property
    def issue_pr(self) -> IssuePrSettings:
        """issue→PR 业务设置视图（DESIGN §4.6 :334；P5 :471）。"""
        return IssuePrSettings(
            tracker=self.tracker,
            workspace=self.workspace,
            agent=self.agent,
            hooks=self.hooks,
            review_feedback=self.review_feedback,
            rules=self.rules,
            telemetry=self.telemetry,
            pr_template=self.pr_template,
            pr_conflict_scan=self.pr_conflict_scan,
            clarifier=self.clarifier,
        )


def _resolve_first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _normalize_secret_value(os.environ.get(name))
        if value:
            return value
    return None


# ---------------------------------------------------------------------------
# Peer Federation (DESIGN_PEER_FEDERATION.md §6.2; PR5)
# ---------------------------------------------------------------------------


@dataclass
class PeerTimeoutsConfig:
    """D23 handshake timeouts (connect 10s + HELLO 30s, retried 3×)."""

    connect: float = 10.0
    hello: float = 30.0


@dataclass
class PeerRateLimitConfig:
    """D25 per-peer token bucket (100 INVOKE/s, burst 200)."""

    rps: float = 100.0
    burst: int = 200


@dataclass
class PeerConfig:
    """Peer Federation daemon settings, env-driven (§6.2).

    ``cli/serve.py`` seeds ``ORCHESTRATORD_PEER_*`` from its flags; the
    peer router/dispatcher read the rest directly from the environment
    so tests and two-daemon runs can flip behavior per process.
    """

    listen: str = "127.0.0.1:9001"
    redis_url: str = "redis://localhost:6379/0"
    # R10: only explicitly accepted peers count; the whitelist below
    # (ORCHESTRATORD_PEER_TRUST) skips the human-accept step for
    # pre-trusted orch_ids.
    trust_list: list[str] = field(default_factory=list)
    # NG4/G10: remote messages are persisted but never auto-scheduled
    # into agent turns unless the operator opts in.
    auto_schedule: bool = False
    # R12: ceiling on concurrently scheduled peer turns.
    max_concurrent_peer_turns: int = 4
    timeouts: PeerTimeoutsConfig = field(default_factory=PeerTimeoutsConfig)
    rate_limit: PeerRateLimitConfig = field(default_factory=PeerRateLimitConfig)
    # D18: at-least-once dedup window keyed by (msg_id, orch_id).
    dedup_window_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> PeerConfig:
        def _f(names: tuple[str, ...], default: float) -> float:
            raw = _resolve_first_env(names)
            try:
                return float(raw) if raw is not None else default
            except ValueError:
                return default

        trust_raw = os.environ.get("ORCHESTRATORD_PEER_TRUST", "")
        return cls(
            listen=os.environ.get("ORCHESTRATORD_PEER_LISTEN", cls.listen),
            redis_url=os.environ.get(
                "ORCHESTRATORD_REDIS_URL", "redis://localhost:6379/0"
            ),
            trust_list=[
                item.strip() for item in trust_raw.split(",") if item.strip()
            ],
            auto_schedule=os.environ.get("ORCHESTRATORD_PEER_AUTO_SCHEDULE") == "1",
            max_concurrent_peer_turns=int(
                _f(
                    ("ORCHESTRATORD_MAX_CONCURRENT_PEER_TURNS",),
                    cls.max_concurrent_peer_turns,
                )
            ),
            timeouts=PeerTimeoutsConfig(
                connect=_f(
                    ("ORCHESTRATORD_PEER_CONNECT_TIMEOUT",),
                    PeerTimeoutsConfig.connect,
                ),
                hello=_f(
                    ("ORCHESTRATORD_PEER_HELLO_TIMEOUT",),
                    PeerTimeoutsConfig.hello,
                ),
            ),
            rate_limit=PeerRateLimitConfig(
                rps=_f(
                    ("ORCHESTRATORD_PEER_RATE_RPS",), PeerRateLimitConfig.rps
                ),
                burst=int(
                    _f(
                        ("ORCHESTRATORD_PEER_RATE_BURST",),
                        PeerRateLimitConfig.burst,
                    )
                ),
            ),
            dedup_window_seconds=_f(
                ("ORCHESTRATORD_PEER_DEDUP_WINDOW",),
                cls.dedup_window_seconds,
            ),
        )

