"""Adapters that execute declarative workflow stages."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .checkpoint import ArtifactResolver
from .validators import ContractValidator
from .workflow_state import StageNode, WorkflowState

if TYPE_CHECKING:
    from ..agent.runner import AgentTaskRunner
    from ..config.schema import AgentConfig, SandboxConfig

logger = logging.getLogger(__name__)


@dataclass
class StageRunResult:
    """StageRunner 执行结果。"""

    stage_id: int
    success: bool
    outputs: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None
    message: str = ""
    # Backend session run_id — the transcript lives under
    # ~/.orchestratord/sessions/<run_id>/, so `run logs` can resolve it.
    run_id: str | None = None


@dataclass
class GateRunResult:
    """GATE 阶段执行结果。"""

    stage_id: int
    approved: bool
    reason: str = ""
    cost_usd: float = 0.0


@dataclass
class DecisionRunResult:
    """DECISION 阶段执行结果。"""

    stage_id: int
    outcome: str  # proceed | pivot | refine | rollback
    next_stage: int | None = None
    cost_usd: float = 0.0


class StageRunner:
    """阶段执行适配器。

    Dispatches generic AgentTask values through AgentTaskRunner:
    - 停滞检测 (stagnation detection)
    - 进度上报 (ProgressSink)
    - 验证管线 (Verification Pipeline)
    - 澄清队列 (ClarificationQueue)
    - 成本追踪
    """

    MAX_RETRIES = 2

    def __init__(
        self,
        agent_runner: Any = None,
        workflow_config: Any = None,
        agent_config: "AgentConfig | None" = None,
        sandbox_config: "SandboxConfig | None" = None,
        workspace_dir: str = "",
        run_dir: str = "",
        tracker: Any = None,
        status_dashboard: Any = None,
        clarification_resolver: Any = None,
        progress_reporter: Any = None,
        llm_client: Any = None,
        diagnostics_callback: Any = None,
        *,
        task_runner: "AgentTaskRunner | None" = None,
        cost_provider: Callable[[], float] | None = None,
    ) -> None:
        self._agent_runner = agent_runner
        self._task_runner: "AgentTaskRunner | None" = task_runner
        self._workflow_config = workflow_config
        self._agent_config = agent_config
        self._sandbox_config = sandbox_config
        self._workspace_dir = workspace_dir
        self._run_dir = run_dir
        self._tracker = tracker
        self._status_dashboard = status_dashboard
        self._clarification_resolver = clarification_resolver
        self._progress_reporter = progress_reporter
        self._llm_client = llm_client
        self._diagnostics_callback = diagnostics_callback
        self._cost_provider = cost_provider
        self._bundle_path: Path | None = None
        self._validator = ContractValidator(
            workspace_dir=self._workspace_dir,
            llm_client=self._llm_client,
        )

    def set_bundle_path(self, bundle_path: Path | str | None) -> None:
        self._bundle_path = Path(bundle_path).resolve() if bundle_path else None

    # ── 公开接口 ──────────────────────────────────────────────────

    async def run(self, stage_node: StageNode, state: WorkflowState) -> StageRunResult:
        """执行 Agent 阶段。

        Build and execute an AgentTask with bounded retries.
        """
        last_error: str | None = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return await self._execute_agent_stage(stage_node, state)
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Stage %s attempt %d/%d failed: %s",
                    stage_node.id,
                    attempt + 1,
                    self.MAX_RETRIES + 1,
                    exc,
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(2**attempt)

        return StageRunResult(
            stage_id=stage_node.id,
            success=False,
            error=last_error,
        )

    async def run_gate(self, stage_node: StageNode, state: WorkflowState) -> GateRunResult:
        """执行 GATE 阶段。"""
        mode = stage_node.gate_mode
        max_retries = stage_node.max_retries

        for attempt in range(max_retries + 1):
            if mode == "auto":
                result = await self._run_auto_gate(stage_node, state)
            elif mode == "threshold":
                result = await self._run_threshold_gate(stage_node, state)
            else:
                return GateRunResult(
                    stage_id=stage_node.id,
                    approved=False,
                    reason=f"GATE stage {stage_node.id} requires manual approval",
                )

            if result.approved or attempt >= max_retries:
                return result

            logger.warning(
                "GATE stage %s attempt %d/%d rejected, retrying...",
                stage_node.id,
                attempt + 1,
                max_retries + 1,
            )
            await asyncio.sleep(2**attempt)

        return GateRunResult(
            stage_id=stage_node.id,
            approved=False,
            reason=f"GATE stage {stage_node.id} failed after {max_retries + 1} attempts",
        )

    async def run_decision(self, stage_node: StageNode, state: WorkflowState) -> DecisionRunResult:
        """执行 DECISION 阶段。"""
        max_retries = stage_node.max_retries

        for attempt in range(max_retries + 1):
            try:
                session = await self._run_agent_task(
                    prompt=self._build_decision_prompt(stage_node, state),
                    stage_node=stage_node,
                    parent_task=(state.run_context or state.issue_context or {}).get("task"),
                    state=state,
                )
                output_text = session.output_text if session else ""
                outcome = self._parse_decision_outcome(output_text, stage_node)
                next_stage = self._resolve_next_stage(outcome, stage_node)

                if outcome != "proceed" or attempt >= max_retries:
                    return DecisionRunResult(
                        stage_id=stage_node.id,
                        outcome=outcome,
                        next_stage=next_stage,
                    )
            except Exception as exc:
                logger.warning(
                    "Decision stage %s attempt %d/%d failed: %s",
                    stage_node.id,
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt)
                else:
                    return DecisionRunResult(stage_id=stage_node.id, outcome="proceed")

        return DecisionRunResult(stage_id=stage_node.id, outcome="proceed")

    async def run_action(self, stage_node: StageNode, state: WorkflowState) -> Any:
        """Execute a registered, backend-neutral workflow action."""
        from .actions import ActionContext, execute_action

        return await execute_action(
            stage_node.uses,
            stage_node.action_config,
            ActionContext(
                run_context=dict(state.run_context or state.issue_context or {}),
                workspace_dir=str(self._workspace_dir),
                stage_results=dict(state.stage_results),
            ),
        )

    # ── Agent stage execution ───────────────────────────────────

    async def _execute_agent_stage(
        self,
        stage_node: StageNode,
        state: WorkflowState,
    ) -> StageRunResult:
        """Execute an agent stage through the generic task protocol."""
        # 记录阶段开始前的总成本，用于计算阶段增量
        cost_before = self._get_total_cost_usd()

        prompt = self._build_stage_prompt(stage_node, state)
        session = await self._run_agent_task(
            prompt=prompt,
            stage_node=stage_node,
            parent_task=(state.run_context or state.issue_context or {}).get("task"),
            state=state,
        )

        if session is None:
            return StageRunResult(
                stage_id=stage_node.id,
                success=False,
                error="AgentRunner returned no session",
            )

        output_text = session.output_text if hasattr(session, "output_text") else ""
        status = session.status if hasattr(session, "status") else "unknown"

        # 计算阶段成本 = 总成本增量
        cost_delta = self._get_total_cost_usd() - cost_before

        return StageRunResult(
            stage_id=stage_node.id,
            success=status == "completed",
            outputs=[output_text] if output_text else [],
            message=output_text,
            cost_usd=max(cost_delta, 0.0),
            error=None if status == "completed" else f"Session status: {status}",
            run_id=getattr(session, "run_id", None),
        )

    async def _run_agent_task(
        self,
        prompt: str,
        stage_node: StageNode,
        parent_task: Any = None,
        state: WorkflowState | None = None,
    ) -> Any:
        """Build a generic work unit and execute it through the capability API."""

        workspace_path = Path(self._workspace_dir) if self._workspace_dir else Path(".")

        # If a task_runner is available, use the new AgentTask path.
        if self._task_runner is not None:
            from ..agent.task import AgentTask

            task = AgentTask(
                id=f"stage-{stage_node.id:02d}",
                kind="workflow_stage",
                conversation_id=getattr(state, "conversation_id", None),
                title=f"[{stage_node.phase}] {stage_node.name}",
                description=prompt,
                context={
                    "stage_id": stage_node.id,
                    "phase": stage_node.phase,
                    "parent_task": parent_task,
                },
                workspace_path=str(workspace_path),
                labels=["workflow-stage", f"workflow-{stage_node.phase}"],
                prompt_override=prompt,
            )
            result = await self._task_runner.run_task(
                task,
                progress_callback=self._forward_progress_event,
            )

            return result

        raise RuntimeError(
            "StageRunner requires an AgentTaskRunner"
        )

    async def _forward_progress_event(self, event: Any) -> None:
        """Forward generic task progress to the legacy workflow sink.

        The workflow engine still owns its dashboard sink.  Keeping this
        translation here prevents the capability runner from importing any
        workflow or dashboard code.
        """
        sink = self._progress_reporter
        if sink is None:
            return
        from ..agent.task import ProgressEventKind

        try:
            if event.kind is ProgressEventKind.TEXT and hasattr(sink, "on_text"):
                sink.on_text(event.text)
            elif event.kind is ProgressEventKind.TEXT_DELTA and hasattr(sink, "on_text_delta"):
                sink.on_text_delta(event.text)
            elif event.kind is ProgressEventKind.TOOL_CALL and hasattr(sink, "on_tool_call"):
                sink.on_tool_call(event.tool_name, event.call_id)
            elif event.kind is ProgressEventKind.TOOL_RESULT and hasattr(sink, "on_tool_result"):
                sink.on_tool_result(event.call_id)
        except Exception:
            logger.debug("workflow progress sink failed", exc_info=True)

    # ── GATE 处理 ─────────────────────────────────────────────────

    async def _run_auto_gate(self, stage_node: StageNode, state: WorkflowState) -> GateRunResult:
        """自动 GATE：基于 validator 结果判定。"""
        if not stage_node.validators:
            return GateRunResult(
                stage_id=stage_node.id, approved=True, reason="no validators, auto-approved"
            )

        results = await self._validator.validate_all(stage_node.validators)
        all_passed = all(r.passed for r in results)

        return GateRunResult(
            stage_id=stage_node.id,
            approved=all_passed,
            reason="All validators passed"
            if all_passed
            else f"Validators failed: {[r.message for r in results if not r.passed]}",
        )

    async def _run_threshold_gate(
        self, stage_node: StageNode, state: WorkflowState
    ) -> GateRunResult:
        """Evaluate a threshold gate through a generic agent task."""
        try:
            prompt = (
                f"Evaluate the following work output and assign a score from 0.0 to 1.0.\n"
                f"Respond with ONLY: score: <number>\n\n"
                f"Stage: {stage_node.name}\n"
                f"Prompt: {stage_node.prompt}\n"
            )
            session = await self._run_agent_task(prompt=prompt, stage_node=stage_node, state=state)
            output_text = session.output_text if session else ""
            score = self._extract_score(output_text)
            approved = score >= stage_node.gate_threshold

            return GateRunResult(
                stage_id=stage_node.id,
                approved=approved,
                reason=f"Score {score:.2f} >= threshold {stage_node.gate_threshold}"
                if approved
                else f"Score {score:.2f} < threshold {stage_node.gate_threshold}",
            )
        except Exception as exc:
            return GateRunResult(
                stage_id=stage_node.id, approved=False, reason=f"Threshold gate error: {exc}"
            )

    # ── DECISION 处理 ─────────────────────────────────────────────

    def _build_decision_prompt(self, stage_node: StageNode, state: WorkflowState) -> str:
        """构建决策阶段提示词。"""
        outcomes = list(stage_node.decision_outcomes.keys())
        return (
            f"Based on the completed stages, decide the next action.\n"
            f"Available outcomes: {', '.join(outcomes)}\n"
            f"Respond with ONE word: the chosen outcome.\n\n"
            f"Stage: {stage_node.name}\n"
            f"Context: {stage_node.prompt}\n"
        )

    def _parse_decision_outcome(self, output_text: str, stage_node: StageNode) -> str:
        """从 LLM 输出中解析决策结果。"""
        text_lower = output_text.strip().lower()
        outcomes = list(stage_node.decision_outcomes.keys())
        for outcome in outcomes:
            if outcome.lower() in text_lower:
                return outcome
        return "proceed"

    def _resolve_next_stage(self, outcome: str, stage_node: StageNode) -> int | None:
        """根据决策结果解析下一个阶段。"""
        decision_spec = stage_node.decision_outcomes.get(outcome, {})
        return decision_spec.get("next")

    # ── 提示词构建 ────────────────────────────────────────────────

    def _build_stage_prompt(self, stage_node: StageNode, state: WorkflowState) -> str:
        """构建阶段提示词。

        结构：
        1. WORKFLOW.md 模板（通过 PromptBuilder.render 获取，含项目上下文、编码规范等）
        2. 当前阶段指令（stage_node.prompt）
        3. 前序阶段输出（供后续阶段参考）
        4. 输出验证要求
        """
        parts = []

        # 1. WORKFLOW.md 基础 prompt（含项目上下文、编码规范、实现方法等）
        base_prompt = self._render_base_prompt(state)
        if base_prompt:
            parts.append(base_prompt)

        # 2. Current stage instruction
        parts.append(f"\n## Current Stage: {stage_node.name}")
        parts.append(f"Phase: {stage_node.phase}")
        stage_agent = (stage_node.agent_config or {}).get("agent")
        if stage_agent:
            parts.append(
                f"\n## Assigned Stage Agent\n"
                f"Execute this stage as sub-agent `@{stage_agent}` via the Agent tool. "
                f"That agent owns the stage skill, tools, and bridge dispatch for this step."
            )
        parts.append(stage_node.prompt or f"Execute stage: {stage_node.name}")

        # Optional business policy supplied by the root task.  The engine
        # deliberately knows nothing about Git, PRs, datasets, or models.
        context = state.run_context or state.issue_context or {}
        root_task = context.get("task")
        stage_instructions = (
            getattr(root_task, "context", {}).get("stage_instructions", [])
            if root_task is not None
            else []
        )
        if stage_instructions:
            parts.append("\n## Workflow Policies")
            parts.extend(f"- {instruction}" for instruction in stage_instructions)

        # 3. 前序阶段输出
        if state.completed_stages:
            parts.append("\n## Completed Stages")
            for sid in state.completed_stages:
                sresult = state.get_stage_result(sid)
                if sresult:
                    parts.append(f"- Stage {sid}: {sresult.status.value}")
                    if sresult.outputs:
                        parts.append(f"  Output: {sresult.outputs[0][:50000]}")

        # 4. 输出验证要求
        if stage_node.validators:
            parts.append("\n## Output Requirements")
            for v in stage_node.validators:
                parts.append(f"- {v.get('type', 'unknown')}: {v}")

        prompt = "\n".join(parts)

        # 解析跨阶段产物引用
        prompt = ArtifactResolver.resolve(
            prompt,
            state=state,
            workspace_dir=str(self._workspace_dir) if self._workspace_dir else "",
        )

        return prompt

    def _render_base_prompt(self, state: WorkflowState) -> str:
        """Render the root task while retaining project guidance."""
        context = state.run_context or state.issue_context or {}
        task = context.get("task")
        if task is None:
            # Degrade to data-only run context when no task object is present.
            if context:
                parts = ["## Task Context"]
                parts.append(f"Title: {context.get('title', 'N/A')}")
                desc = context.get("description", "")
                if desc:
                    parts.append(f"Description: {desc}")
                return "\n".join(parts)
            return ""

        try:
            from ..prompt_builder import PromptBuilder

            return PromptBuilder.render(task)
        except Exception as exc:
            logger.warning("PromptBuilder.render failed, using fallback: %s", exc)
            # Fall back to the task's title and description.
            title = getattr(task, "title", "") or context.get("title", "")
            desc = getattr(task, "description", "") or context.get("description", "")
            return f"## Task: {title}\n\n{desc}"

    # ── 工具方法 ──────────────────────────────────────────────────

    def _get_total_cost_usd(self) -> float:
        """Return the injected workflow cost total, if available.

        StageRunner deliberately has no dependency on an agent runtime's
        global bootstrap state. The workflow owner may inject a provider;
        callers that do not have one retain the old zero-cost fallback.
        """
        if self._cost_provider is None:
            return 0.0
        try:
            return float(self._cost_provider())
        except Exception:  # noqa: BLE001
            logger.debug("cost provider failed", exc_info=True)
            return 0.0

    @staticmethod
    def _extract_score(text: str) -> float:
        """从 LLM 输出中提取评分 (0.0-1.0)。"""
        import re

        match = re.search(r"(?:score|评分)[:\s]*([0-9]*\.?[0-9]+)", text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
        return 0.0
