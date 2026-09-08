"""ModeDispatcher — 模式注册/选择/路由（DESIGN §5 机制职责表第 7 行）。

从 orchestrator.py 迁入的纯机制段：按 ``workflow.modes.enabled`` 注册
ModeRunner、构建 ModeSelector。引用现有 ``modes/``、``mode_selector.py``
不改；Kernel/Orchestrator 均经本模块装配。
"""

from __future__ import annotations

import logging

from .. import modes as _modes
from ..backend_runner import BackendRunner
from ..config.schema import WorkflowConfig
from ..mode_router import HeuristicRouter, LLMRouter, Router
from ..mode_selector import ModeSelector
from ..modes.coordinator import CoordinatorModeRunner
from ..modes.debate import DebateModeRunner
from ..modes.pipeline import PipelineModeRunner
from ..modes.single import SingleModeRunner
from ..modes.swarm import SwarmModeRunner

logger = logging.getLogger(__name__)


def register_collaboration_modes(
    workflow: WorkflowConfig, agent_runner: BackendRunner
) -> None:
    """Register the ``ModeRunner`` instances that match ``modes.enabled``.

    ``single`` is always registered (it's the safe fallback). Other
    modes are registered only when listed in ``workflow.modes.enabled``
    so an operator can disable a mode without removing its code.
    """
    # Always register "single" — it's both the default fallback and
    # the run mode for legacy / followup / review_followup paths.
    _modes.register("single", SingleModeRunner(agent_runner))

    enabled = {m.strip().lower() for m in workflow.modes.enabled if m}
    if "pipeline" in enabled:
        stages = tuple(workflow.modes.pipeline_stages)
        max_retries = int(getattr(workflow.modes, "pipeline_max_retries_per_stage", 1))
        stage_models = dict(getattr(workflow.modes, "pipeline_stage_models", None) or {})
        stage_max_turns = dict(getattr(workflow.modes, "pipeline_stage_max_turns", None) or {})
        stage_specs = dict(getattr(workflow.modes, "pipeline_stage_specs", None) or {})
        handoff = str(getattr(workflow.modes, "pipeline_handoff", "prompt"))
        try:
            _modes.register(
                "pipeline",
                PipelineModeRunner(
                    agent_runner,
                    stages=stages,
                    max_retries_per_stage=max_retries,
                    stage_models=stage_models,
                    stage_max_turns=stage_max_turns,
                    stage_specs=stage_specs,
                    handoff=handoff,
                ),
            )
        except ValueError as exc:
            # Bad stage_specs (e.g. kind=pipeline nested). Fall back
            # to a spec-less pipeline so the daemon keeps running.
            logger.warning(
                "Pipeline registration failed (%s) — registering without stage_specs",
                exc,
            )
            _modes.register(
                "pipeline",
                PipelineModeRunner(
                    agent_runner,
                    stages=stages,
                    max_retries_per_stage=max_retries,
                    stage_models=stage_models,
                    stage_max_turns=stage_max_turns,
                    stage_specs={},
                    handoff=handoff,
                ),
            )
            stage_specs = {}
        logger.info(
            "Collaboration mode registered: pipeline (stages=%s, "
            "max_retries_per_stage=%d, stage_models=%s, "
            "stage_max_turns=%s, stage_specs=%s, handoff=%s)",
            stages,
            max_retries,
            stage_models or "(none)",
            stage_max_turns or "(none)",
            stage_specs or "(none)",
            handoff,
        )
    if "coordinator" in enabled:
        _modes.register("coordinator", CoordinatorModeRunner(agent_runner))
        logger.info("Collaboration mode registered: coordinator")
    if "swarm" in enabled:
        _modes.register(
            "swarm",
            SwarmModeRunner(
                agent_runner,
                max_subtasks=workflow.modes.swarm_max_subtasks,
                max_parallel=workflow.modes.swarm_max_parallel,
                max_waves=workflow.modes.swarm_max_waves,
            ),
        )
        logger.info(
            "Collaboration mode registered: swarm (max_subtasks=%d, "
            "max_parallel=%d, max_waves=%d)",
            workflow.modes.swarm_max_subtasks,
            workflow.modes.swarm_max_parallel,
            workflow.modes.swarm_max_waves,
        )
    if "debate" in enabled:
        proposers = tuple(
            getattr(workflow.modes, "debate_proposers", None) or ("proposer_a", "proposer_b")
        )
        judge_model = getattr(workflow.modes, "debate_judge_model", None)
        isolation = getattr(workflow.modes, "debate_isolation", "reset")
        proposer_models = dict(getattr(workflow.modes, "debate_proposer_models", None) or {})
        parallel = bool(getattr(workflow.modes, "debate_parallel", False))
        judge_mode = str(getattr(workflow.modes, "debate_judge_mode", "pick"))
        try:
            _modes.register(
                "debate",
                DebateModeRunner(
                    agent_runner,
                    proposers=proposers,
                    judge_model=judge_model,
                    isolation=isolation,
                    proposer_models=proposer_models,
                    parallel=parallel,
                    judge_mode=judge_mode,
                ),
            )
        except ValueError as exc:
            # Most likely: parallel=True without isolation=worktree,
            # or an invalid judge_mode. Fall back to safe defaults so
            # the daemon keeps running.
            logger.warning(
                "Debate registration failed (%s) — registering with "
                "parallel=False, isolation='%s', judge_mode='pick'",
                exc,
                isolation,
            )
            _modes.register(
                "debate",
                DebateModeRunner(
                    agent_runner,
                    proposers=proposers,
                    judge_model=judge_model,
                    isolation=isolation,
                    proposer_models=proposer_models,
                    parallel=False,
                    judge_mode="pick",
                ),
            )
            parallel = False
            judge_mode = "pick"
        logger.info(
            "Collaboration mode registered: debate (proposers=%s, "
            "judge_model=%s, isolation=%s, parallel=%s, "
            "proposer_models=%s, judge_mode=%s)",
            proposers,
            judge_model or "(default)",
            isolation,
            parallel,
            proposer_models or "(none)",
            judge_mode,
        )


def build_mode_selector(workflow: WorkflowConfig) -> ModeSelector:
    """Construct ``ModeSelector`` with the configured router backend."""
    router: Router | None
    kind = workflow.modes.router_kind
    if kind == "heuristic":
        router = HeuristicRouter()
        logger.info("ModeSelector: router=HeuristicRouter")
    elif kind == "llm":
        router = LLMRouter(
            model=workflow.modes.router_model,
            endpoint=workflow.modes.router_endpoint,
            api_key_env_var=workflow.modes.router_api_key_env,
            timeout_seconds=workflow.modes.router_timeout_seconds,
        )
        logger.info(
            "ModeSelector: router=LLMRouter(model=%s, endpoint=%s, "
            "api_key_env=%s, timeout=%.1fs)",
            workflow.modes.router_model,
            workflow.modes.router_endpoint,
            workflow.modes.router_api_key_env,
            workflow.modes.router_timeout_seconds,
        )
    else:
        router = None
        logger.info("ModeSelector: no router configured (kind=%s)", kind)

    default_mode = workflow.modes.default
    try:
        return ModeSelector(
            default_mode=default_mode,
            router=router,
            min_confidence=workflow.modes.router_min_confidence,
        )
    except ValueError as exc:
        # workflow.md misconfiguration — fall back to safe defaults
        # instead of crashing the daemon at startup.
        logger.warning("ModeSelector construction failed (%s); using defaults", exc)
        return ModeSelector()
