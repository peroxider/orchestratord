# AgentTask 抽象层 — 详细设计文档

> **状态：** 草案，待评审
> **目标：** 将 agent 编排（功能层）与 issue→PR（业务层）解耦，使不同功能层与业务层可以自由拼接
> **原则：** 最小化改动面、保持向后兼容、SPI 层不动

---

## 1. 问题定义

当前架构中，agent 编排能力与 issue-to-PR 管道是**同一条代码路径**：

```
TrackerAdapter.fetch_candidates()
  → Orchestrator._poll_and_dispatch()
    → Orchestrator._run_issue(session)       ← 上帝方法，~200 行
      → AgentRunner.run(session, workflow, tracker, ...)
      → GitSyncService.sync(session)
      → IssueRegistry.mark_synced()
```

这导致：
- **无法复用 agent 编排能力**：想做"定时代码巡检""CI 失败自动修复""批量重构"等非 issue 场景时，必须伪造 Issue 对象（`StageRunner._run_synthetic_issue` 已经在这样做）
- **AgentRunner 接口臃肿**：`run()` 接受 `tracker`、`comment_tracker`、`clarification_resolver`、`status_dashboard` 等 issue 特有概念
- **AgentSession 绑定 Issue**：`session.issue` 是必填字段，没有任何 agent 运行可以脱离 Issue
- **GitSync 内联调用**：commit/push/PR 创建与 agent 执行在同一个方法里，无法独立替换或跳过

## 2. 目标架构：功能层与业务层分离

### 2.1 核心概念

```
┌──────────────────────────────────────────────────────────────┐
│                    业务层 (Business Layer)                    │
│                                                              │
│  ┌──────────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Issue-to-PR      │  │ CI Auto-Fix  │  │ Code Audit    │  │
│  │ pipeline         │  │ pipeline     │  │ pipeline      │  │
│  │                  │  │              │  │               │  │
│  │ poll → task →    │  │ webhook →    │  │ cron →        │  │
│  │ run → git → PR   │  │ task → run   │  │ task → run    │  │
│  │ → comment        │  │ → report     │  │ → report      │  │
│  └────────┬─────────┘  └──────┬───────┘  └──────┬────────┘  │
│           │                   │                  │           │
│           │  每个业务层通过 AgentTask 描述"要做什么"           │
│           │  通过 AgentTaskResult 获取"做得怎么样"            │
│           │  业务层决定"做完之后怎么办"                       │
│           └───────────────┬───┴──────────────────┘           │
│                           │                                  │
│              AgentTaskRunner.run_task(task)                  │
│                           │                                  │
└───────────────────────────┼──────────────────────────────────┘
                            │
┌───────────────────────────┼──────────────────────────────────┐
│                           │                                  │
│              功能层 (Capability Layer)                       │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              AgentTaskRunner Protocol                 │   │
│  │         run_task(task) → AgentTaskResult              │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─────────────────┐  ┌─────────────────┐                   │
│  │ AgentRunner     │  │ BackendRunner   │                   │
│  │ (clawcodex)     │  │ (SPI: codex/    │                   │
│  │                 │  │  dsh/opencode/  │                   │
│  │                 │  │  hermes)        │                   │
│  └─────────────────┘  └─────────────────┘                   │
│                                                              │
│  职责：prompt 构建、agent 执行、事件流、审批、中断、resume     │
│  不感知：Issue、Tracker、Git、PR、CI、Cron                   │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 拼接关系

功能层和业务层之间通过 `AgentTaskRunner` 协议对接，**任意业务层可以对接任意功能层实现**：

```
Issue-to-PR ─┐                    ┌── AgentRunner (clawcodex)
             │                    │
CI Auto-Fix ─┼── AgentTaskRunner ─┼── BackendRunner (codex)
             │                    │
Code Audit ─┘                    └── BackendRunner (opencode)
```

拼接的**唯一契约**是：
- 业务层构造 `AgentTask`，调用 `run_task()`
- 功能层返回 `AgentTaskResult`
- 业务层通过 `progress_callback` 接收运行时事件

### 2.3 关键原则

1. **功能层不感知业务** — `AgentRunner` / `BackendRunner` 不知道"这是一个 issue"还是"这是一个 CI 修复"
2. **业务层不感知实现** — Issue-to-PR pipeline 不知道底层是 clawcodex 还是 opencode 在跑
3. **通过 context 扩展** — 业务特有数据放在 `AgentTask.context` 里，功能层只做透传不做假设
4. **通过回调消费事件** — 业务层通过 `progress_callback` 接收事件，自行决定是写 dashboard、发 IM 还是记录 journal

### 2.4 拼接机制

业务层与功能层的拼接发生在 `Orchestrator` 中，通过配置决定"哪个业务管线用哪个 runner"：

```python
# orchestrator.py — 拼接点

class Orchestrator:
    def __init__(self, ...):
        # 功能层：可插拔的 runner
        self._agent_runner: AgentTaskRunner = AgentRunner(...)
        self._backend_runner: AgentTaskRunner | None = BackendRunner(...)

        # 业务层：可插拔的 pipeline
        self._pipelines: dict[str, PipelineHandler] = {
            "issue-to-pr": self._run_issue_pipeline,
            # future:
            # "ci-fix": self._run_ci_fix_pipeline,
            # "code-audit": self._run_code_audit_pipeline,
        }

    def _resolve_runner(self, task: AgentTask) -> AgentTaskRunner:
        """Select the capability layer for a given task."""
        # Per-task kind or per-workflow config decides which runner
        if self._backend_runner is not None:
            return self._backend_runner
        return self._agent_runner

    async def run_task(self, task: AgentTask) -> AgentTaskResult:
        """Public entry point: any business layer can call this."""
        runner = self._resolve_runner(task)
        return await runner.run_task(task, ...)
```

**新增业务层的步骤：**

1. 定义 `AgentTask.kind`（如 `"ci-fix"`）
2. 实现 `PipelineHandler`：构造 `AgentTask` → 调用 `run_task()` → 后处理
3. 注册到 `self._pipelines`
4. 功能层**零改动**

**切换功能层实现的步骤：**

1. 配置中指定 backend（如 `--backend opencode`）
2. `_resolve_runner()` 返回对应的 `AgentTaskRunner`
3. 业务层**零改动**

---

## 3. 核心数据结构

### 3.1 AgentTask

```python
# src/orchestratord/agent_task.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTask:
    """A unit of work dispatched to an AI coding agent.

    AgentTask is the generic abstraction that sits between "something
    that needs doing" and the agent execution layer.  It intentionally
    knows nothing about issues, pull requests, git, or trackers.

    Different workflow kinds populate different fields:

    ``kind="issue"``
        ``title`` = issue title, ``description`` = issue body,
        ``context["issue_id"]`` = tracker ID,
        ``context["issue_identifier"]`` = HUMAN-READABLE key

    ``kind="workflow_stage"``
        ``title`` = "[phase] stage name", ``description`` = stage prompt,
        ``context["stage_id"]``, ``context["phase"]``,
        ``context["parent_issue"]`` = original Issue (for template rendering)

    ``kind="review_followup"``
        ``title`` = issue title, ``description`` = review feedback,
        ``context["pull_request"]`` = PullRequestRef,
        ``context["feedback"]`` = list[PullRequestFeedback]

    ``kind="agent_rebase"``
        ``title`` = issue title, ``description`` = rebase instructions,
        ``context["branch_name"]``, ``context["base_branch"]``,
        ``context["conflict_files"]``

    ``kind="ci_fix"`` (future)
        ``title`` = "CI Failure: <job>", ``description`` = build log,
        ``context["job_name"]``, ``context["build_url"]``
    """

    # ── Identity ──────────────────────────────────────────────
    id: str
    kind: str = "generic"

    # ── Core content ──────────────────────────────────────────
    title: str = ""
    description: str = ""

    # ── Extensible context (workflow-specific) ────────────────
    # Keys are defined per-kind.  Layer 1 treats this as opaque
    # data for template rendering.  Layer 2 populates it.
    context: dict[str, Any] = field(default_factory=dict)

    # ── Workspace ─────────────────────────────────────────────
    workspace_path: str = ""

    # ── Metadata ──────────────────────────────────────────────
    labels: list[str] = field(default_factory=list)
    priority: int | None = None
    attempt: int = 1
    previous_run_ids: list[str] = field(default_factory=list)

    # ── Lifecycle hints (overrides per-workflow defaults) ─────
    max_turns: int | None = None
    timeout_seconds: float | None = None

    # ── Prompt override ───────────────────────────────────────
    # When set, bypasses PromptBuilder entirely.  Used by
    # StageRunner for synthetic stage prompts.
    prompt_override: str | None = None

    def to_template_dict(self) -> dict[str, Any]:
        """Render-friendly dict for Jinja2 templates.

        Templates can access ``{{ task.title }}``, ``{{ task.kind }}``,
        and any ``{{ task.context.xxx }}`` key.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "labels": self.labels,
            "priority": self.priority,
            "attempt": self.attempt,
            "context": dict(self.context),
        }
```

### 3.2 AgentTaskResult

```python
# src/orchestratord/agent_task.py (续)

@dataclass
class AgentTaskResult:
    """Result of executing an AgentTask through the orchestration layer.

    This is the *only* output from Layer 1.  Layer 2 interprets it
    according to the task kind and decides what to do next (git sync,
    PR update, retry, etc.).
    """

    task_id: str
    kind: str = "generic"

    # ── Terminal status ───────────────────────────────────────
    # One of: "completed", "failed", "stagnation", "loop_detected",
    #         "max_turns_exceeded", "read_only_loop", "premise_not_met",
    #         "no_changes_produced", "empty_branch_no_commits",
    #         "interrupted", "rate_limited"
    status: str = "completed"

    # ── Output ────────────────────────────────────────────────
    output_text: str = ""
    turn_count: int = 0
    tool_count: int = 0

    # ── End reason (detailed) ─────────────────────────────────
    session_end_reason: str | None = None
    session_end_summary: str = ""

    # ── Verification ──────────────────────────────────────────
    verification_status: str | None = None
    verification_output: str | None = None

    # ── Artifacts ─────────────────────────────────────────────
    report_path: str | None = None
    run_id: str | None = None

    # ── Cost ──────────────────────────────────────────────────
    cost_usd: float = 0.0

    # ── Error ─────────────────────────────────────────────────
    error: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == "completed"

    @property
    def is_terminal_failure(self) -> bool:
        return self.status in (
            "failed", "premise_not_met", "no_changes_produced",
            "empty_branch_no_commits",
        )
```

### 3.3 ProgressEvent（进度回调）

```python
# src/orchestratord/agent_task.py (续)

from enum import Enum


class ProgressEventKind(str, Enum):
    TEXT = "text"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TURN_COMPLETE = "turn_complete"
    SESSION_COMPLETE = "session_complete"
    ERROR = "error"


@dataclass
class ProgressEvent:
    """Emitted by Layer 1 during agent execution.

    Layer 2 subscribes to these for dashboards, IM notifications,
    and state-journal writes — without Layer 1 knowing about any of
    those consumers.
    """

    kind: ProgressEventKind
    task_id: str
    turn_number: int | None = None
    tool_count: int | None = None
    text: str = ""
    tool_name: str = ""
    call_id: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
```

---

## 4. AgentTaskRunner 协议

```python
# src/orchestratord/agent_task_runner.py

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from .agent_task import AgentTask, AgentTaskResult, ProgressEvent


class AgentTaskRunner(Protocol):
    """Protocol for executing an AgentTask.

    Both ``AgentRunner`` (clawcodex path) and ``BackendRunner`` (SPI path)
    implement this protocol.  The Orchestrator depends on the protocol,
    not on either concrete implementation.
    """

    async def run_task(
        self,
        task: AgentTask,
        *,
        progress_callback: Callable[[ProgressEvent], Awaitable[None]] | None = None,
        diagnostics_callback: Callable[[AgentTaskResult], None] | None = None,
    ) -> AgentTaskResult:
        """Execute *task* and return its result.

        Args:
            task: The task to execute.
            progress_callback: If set, called for each event during
                execution (text deltas, tool calls, turn completes, etc.).
                Layer 1 does not know or care who consumes these.
            diagnostics_callback: If set, called once with the final
                result before returning.

        Returns:
            AgentTaskResult with the terminal status and outputs.
        """
        ...
```

**关键设计决策：**

1. **协议而非 ABC** — `Protocol` 允许 `AgentRunner` 和 `BackendRunner` 不需要显式继承，只要实现了 `run_task` 方法即可。这对渐进迁移至关重要。

2. **回调而非直接依赖** — `progress_callback` 替代了当前 `AgentRunner.run()` 的 `tracker`、`status_dashboard`、`progress_reporter`、`comment_tracker` 等参数。Layer 1 只发射事件，Layer 2 决定如何消费。

3. **无 workflow 参数** — 当前 `AgentRunner.run(session, workflow, ...)` 接受 `WorkflowConfig`。在 `AgentTaskRunner` 中，`AgentTask` 本身携带了足够的上下文（`max_turns`、`timeout_seconds`、`prompt_override`）。WorkflowConfig 的其余部分（agent model、sandbox、approval policy）在构造 runner 时已注入。

---

## 5. Issue → AgentTask 适配器

```python
# src/orchestratord/issue_to_task.py

"""Convert between Issue (tracker domain) and AgentTask (orchestration domain)."""

from __future__ import annotations

from .agent_task import AgentTask
from .issue import Issue


def issue_to_agent_task(
    issue: Issue,
    *,
    attempt: int = 1,
    previous_run_ids: list[str] | None = None,
    workspace_path: str = "",
    max_turns: int | None = None,
    timeout_seconds: float | None = None,
    clarification_question: str | None = None,
    clarification_answer: str | None = None,
    clarification_source: str | None = None,
    conflict_files: tuple[str, ...] | None = None,
    prompt_override: str | None = None,
) -> AgentTask:
    """Convert an Issue to a generic AgentTask.

    This is the **only** place where Issue fields are mapped to
    AgentTask fields.  If a new task kind needs Issue data, it goes
    through this function — not by accessing Issue directly.
    """
    context: dict[str, object] = {
        "issue_id": issue.id,
        "issue_identifier": issue.identifier,
        "issue_url": issue.url,
        "issue_state": issue.state,
        "issue_author_login": issue.author_login,
        "issue_branch_name": issue.branch_name,
        "issue_python_executable": issue.python_executable,
    }

    if clarification_question:
        context["clarification_question"] = clarification_question
    if clarification_answer:
        context["clarification_answer"] = clarification_answer
    if clarification_source:
        context["clarification_source"] = clarification_source
    if conflict_files:
        context["conflict_files"] = list(conflict_files)

    return AgentTask(
        id=issue.id or "",
        kind="issue",
        title=issue.title or "",
        description=issue.description or "",
        context=context,
        workspace_path=workspace_path,
        labels=list(issue.labels) if issue.labels else [],
        priority=issue.priority,
        attempt=attempt,
        previous_run_ids=list(previous_run_ids) if previous_run_ids else [],
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        prompt_override=prompt_override,
    )
```

---

## 6. PromptBuilder 改造

### 6.1 当前状态

`PromptBuilder.render()` 的第一个参数是 `issue: Any`，内部通过 `issue.to_dict()` 获取数据。模板使用 `{{ issue.identifier }}`、`{{ issue.title }}` 等。

### 6.2 目标状态

`PromptBuilder` 接受 `AgentTask` 作为主要输入，模板使用 `{{ task.title }}` 替代 `{{ issue.title }}`。同时保留对旧 Issue 对象的兼容。

```python
# src/orchestratord/prompt_builder.py — 关键改动

class PromptBuilder:

    @staticmethod
    def render(
        task: AgentTask | Any,  # AgentTask or legacy Issue
        attempt: int | None = None,
        clarification_context: str | None = None,
        pending_question: str | None = None,
        options: list[str] | None = None,
        session: Any | None = None,
        python_executable: str | None = None,
        previous_run_ids: list[str] | None = None,
        conflict_files: tuple[str, ...] | list[str] | None = None,
    ) -> str:
        # Detect input type
        if isinstance(task, AgentTask):
            task_dict = task.to_template_dict()
        elif hasattr(task, "to_dict"):
            task_dict = task.to_dict()
            task_dict = {
                "id": task_dict.get("id"),
                "kind": "issue",
                "title": task_dict.get("title"),
                "description": task_dict.get("description"),
                "context": task_dict,
            }
        else:
            task_dict = dict(task)

        context = {
            "attempt": attempt,
            "task": _to_jinja_value(task_dict),
            # Backward compat: keep "issue" key for existing templates
            "issue": _to_jinja_value(task_dict.get("context", task_dict)),
            "clarification": clarification_context,
            "pending_question": pending_question,
            "options": options,
        }
        # ... rest of rendering unchanged
```

### 6.3 模板迁移

现有 `WORKFLOW.md` 模板中的 `{{ issue.identifier }}` 改为 `{{ task.context.issue_identifier }}`。

**默认模板改动：**

```diff
- Issue: {{ issue.identifier }} - {{ issue.title }}
+ Task: {{ task.title }}
+ {% if task.kind == "issue" %}
+ Issue: {{ task.context.issue_identifier }}
+ {% endif %}
```

由于 `context` 中保留了 `issue` 兼容键，现有模板不会立即 break。迁移分两步：
1. **Phase A**（本文档范围）：`PromptBuilder` 同时接受 `AgentTask` 和 `Issue`，模板同时支持 `{{ task.* }}` 和 `{{ issue.* }}`
2. **Phase B**（后续）：废弃 `{{ issue.* }}`，所有模板迁移到 `{{ task.* }}`

---

## 7. AgentRunner 改造

### 7.1 当前签名

```python
async def run(self, session: AgentSession, workflow: WorkflowConfig,
              status_dashboard=None, tracker=None, comment_tracker=None,
              clarification_resolver=None, progress_reporter=None,
              diagnostics_callback=None) -> None:
```

### 7.2 目标签名

```python
# AgentRunner 实现 AgentTaskRunner Protocol
async def run_task(
    self,
    task: AgentTask,
    *,
    progress_callback: Callable[[ProgressEvent], Awaitable[None]] | None = None,
    diagnostics_callback: Callable[[AgentTaskResult], None] | None = None,
) -> AgentTaskResult:
```

### 7.3 内部实现要点

`run_task()` 内部：
1. 从 `task` 构建 `AgentSession`（不再需要外部传入 Issue）
2. 从 `task.prompt_override` 或 `PromptBuilder.render(task)` 构建 prompt
3. 执行 agent 循环（现有逻辑不变）
4. 将 `ProgressEvent` 通过 `progress_callback` 发射
5. 返回 `AgentTaskResult`

```python
async def run_task(self, task, *, progress_callback=None, diagnostics_callback=None):
    # 1. 构建内部 session（不再需要外部 Issue）
    workspace = Workspace(
        path=Path(task.workspace_path) if task.workspace_path else Path("."),
        issue_identifier=task.context.get("issue_identifier", task.id),
        issue_id=task.context.get("issue_id", task.id),
    )
    session = AgentSession(
        issue=Issue(
            id=task.context.get("issue_id", task.id),
            identifier=task.context.get("issue_identifier"),
            title=task.title,
            description=task.description,
            labels=task.labels,
        ),
        task=task,
        workspace=workspace,
        run_kind=task.kind,
        run_id=task.id,
        attempt=task.attempt,
        previous_run_ids=task.previous_run_ids,
    )

    # 2. 构建 prompt
    if task.prompt_override:
        prompt = task.prompt_override
    else:
        prompt = PromptBuilder.render(task, attempt=task.attempt, ...)

    # 3. 执行 agent 循环（现有逻辑不变）
    # ... 现有 _run_impl 逻辑 ...

    # 4. 构建结果
    return AgentTaskResult(
        task_id=task.id,
        kind=task.kind,
        status=session.status,
        output_text=session.output_text,
        turn_count=session.turn_count,
        tool_count=session.tool_count,
        session_end_reason=session.session_end_reason,
        session_end_summary=session.session_end_summary,
        verification_status=session.verification_status,
        verification_output=session.verification_output,
        report_path=session.report_path,
        run_id=session.run_id,
    )
```

### 7.4 向后兼容

保留旧的 `run()` 方法作为 deprecated wrapper：

```python
async def run(self, session, workflow, **kwargs):
    """Deprecated: use run_task() instead."""
    task = issue_to_agent_task(
        session.issue,
        attempt=session.attempt,
        workspace_path=str(session.workspace.path),
        ...
    )
    result = await self.run_task(
        task,
        progress_callback=self._build_legacy_callback(kwargs),
        diagnostics_callback=kwargs.get("diagnostics_callback"),
    )
    # 回写 session 状态（兼容旧调用方）
    session.status = result.status
    session.output_text = result.output_text
    ...
```

---

## 8. BackendRunner 改造

与 `AgentRunner` 相同：新增 `run_task()` 实现 `AgentTaskRunner` 协议，保留旧 `run()` 作为 wrapper。

`BackendRunner` 的改造更简单，因为它已经通过 SPI 与后端通信，`run_task()` 主要是参数的重新映射。

---

## 9. Orchestrator 拆分

### 9.1 当前 `_run_issue()` 结构

```
_run_issue(session)
  ├─ before_run hook
  ├─ repro gate
  ├─ dispatch to AgentRunner / WorkflowOrchestrator
  ├─ cannot_proceed check + comment
  ├─ workspace change verification
  ├─ git_sync (commit + push + PR)
  └─ registry update
```

### 9.2 目标拆分

**Layer 1: `_run_agent_task()`（新增）**

```python
# orchestrator.py

async def _run_agent_task(
    self,
    task: AgentTask,
    *,
    progress_callback: Callable | None = None,
) -> AgentTaskResult:
    """Run an agent task through the configured runner.  Pure Layer 1."""
    runner = self._resolve_runner(task)  # AgentRunner or BackendRunner
    return await runner.run_task(
        task,
        progress_callback=progress_callback,
        diagnostics_callback=self._update_run_diagnostics_for_task,
    )
```

**Layer 2: `_run_issue()` 变为 thin orchestrator**

```python
async def _run_issue(self, session: AgentSession) -> None:
    """Issue-to-PR pipeline: convert issue → task → run → git → PR."""
    async with self._semaphore:
        # 1. before_run hook
        await self.workspace.run_before_run_hook(session.workspace, session.issue)

        # 2. Convert Issue → AgentTask
        task = issue_to_agent_task(
            session.issue,
            attempt=session.attempt,
            previous_run_ids=session.previous_run_ids,
            workspace_path=str(session.workspace.path),
            max_turns=self.workflow.agent.max_turns,
            timeout_seconds=self.workflow.agent.run_timeout_ms / 1000.0,
            clarification_question=session.clarification_question,
            clarification_answer=session.clarification_answer,
            conflict_files=session.conflict_files,
        )

        # 3. Repro gate (unchanged)
        if self._repro_gate_applies(session):
            gate_open = await self._run_repro_gate(session, ...)
            if not gate_open:
                return

        # 4. Run agent via Layer 1
        progress_sink = self._build_session_sink(session.issue.id or "")
        result = await self._run_agent_task(
            task,
            progress_callback=self._progress_event_to_sink(progress_sink),
        )

        # 5. Post-run: cannot_proceed, git_sync, registry (unchanged)
        if result.status == "premise_not_met":
            await self._handle_cannot_proceed(session, result)
            return
        if result.status == "no_changes_produced":
            await self._handle_no_changes(session, result)
            return

        sync_result = await self.git_sync.sync(session, mode=self._sync_mode(session))
        self._registry.update_report(session.issue.id or "", ...)
```

### 9.3 非 Issue 场景的入口

新增 `Orchestrator.run_task()` 公开方法，允许外部直接提交 `AgentTask`：

```python
async def run_task(self, task: AgentTask) -> AgentTaskResult:
    """Run a generic agent task.  No issue, no git sync, no PR."""
    return await self._run_agent_task(task)
```

---

## 10. StageRunner 改造

### 10.1 当前

`StageRunner._run_synthetic_issue()` 构建假的 `Issue` 对象，然后调用 `AgentRunner.run()`。

### 10.2 目标

`StageRunner` 构建 `AgentTask(kind="workflow_stage")`，通过 `AgentTaskRunner.run_task()` 执行。

```python
# workflow_engine/stage_runner.py

async def _execute_agent_stage(self, stage_node, state) -> StageRunResult:
    task = AgentTask(
        id=f"stage-{stage_node.id:02d}",
        kind="workflow_stage",
        title=f"[{stage_node.phase}] {stage_node.name}",
        description=self._build_stage_prompt(stage_node, state),
        context={
            "stage_id": stage_node.id,
            "phase": stage_node.phase,
            "parent_issue": state.issue_context.get("_issue"),
        },
        workspace_path=self._workspace_dir,
        labels=[f"workflow-stage", f"workflow-{stage_node.phase}"],
    )

    result = await self._task_runner.run_task(
        task,
        progress_callback=self._make_progress_callback(),
    )

    return StageRunResult(
        stage_id=stage_node.id,
        success=result.is_success,
        outputs=[result.output_text] if result.output_text else [],
        cost_usd=result.cost_usd,
        error=result.error,
    )
```

### 10.3 依赖注入

`StageRunner.__init__` 接受 `AgentTaskRunner` 而非 `AgentRunner`：

```diff
class StageRunner:
    def __init__(
        self,
-       agent_runner: "AgentRunner",
+       task_runner: AgentTaskRunner,
        ...
    ):
-       self._agent_runner = agent_runner
+       self._task_runner = task_runner
```

---

## 11. WorkflowOrchestrator 改造

### 11.1 当前

```python
async def run_for_issue(self, issue: Issue, workspace_path: str = "",
                        from_stage: int | None = None) -> WorkflowResult:
```

### 11.2 目标

```python
async def run_for_task(self, task: AgentTask, *,
                       from_stage: int | None = None) -> WorkflowResult:
    """Execute the declarative workflow for a generic AgentTask."""
    self._engine.state.issue_context = {
        "id": task.id,
        "identifier": task.context.get("issue_identifier"),
        "title": task.title,
        "description": task.description,
        "labels": task.labels,
        "_issue": task.context.get("parent_issue"),
    }
    return await self.run(from_stage=from_stage)

# 向后兼容
async def run_for_issue(self, issue, **kwargs):
    task = issue_to_agent_task(issue, ...)
    return await self.run_for_task(task, **kwargs)
```

---

## 12. AgentSession 改造

`AgentSession` 保留 `issue: Issue` 字段以保持向后兼容，但改为可选：

```diff
@dataclass
class AgentSession:
-   issue: Issue
+   issue: Issue  # DEPRECATED: use task instead
+   task: AgentTask | None = None  # NEW: generic task
    workspace: Workspace
    ...
```

当 `task` 为 None 时，runner 从 `issue` 构建 `AgentTask`。新代码优先设置 `task`。

---

## 13. 业务层示例：三种管线的拼接方式

### 13.1 示例 A：Issue-to-PR（现有业务）

```python
# orchestrator.py — 业务层：issue-to-PR pipeline

async def _run_issue_pipeline(self, session: AgentSession) -> None:
    """业务层：issue → PR 的完整生命周期"""
    async with self._semaphore:
        await self.workspace.run_before_run_hook(session.workspace, session.issue)

        # 1. 业务层构造 AgentTask（描述"要做什么"）
        task = issue_to_agent_task(
            session.issue,
            attempt=session.attempt,
            workspace_path=str(session.workspace.path),
            max_turns=self.workflow.agent.max_turns,
            timeout_seconds=self.workflow.agent.run_timeout_ms / 1000.0,
        )

        # 2. 调用功能层（不感知底层是 clawcodex 还是 opencode）
        result = await self.run_task(task)

        # 3. 业务层后处理（git sync / PR / registry — 功能层不感知）
        if result.status == "premise_not_met":
            await self._handle_cannot_proceed(session, result)
            return

        sync_result = await self.git_sync.sync(session)
        self._registry.mark_synced(session.issue.id, sync_result)
```

### 13.2 示例 B：CI 失败自动修复（未来业务）

```python
# orchestrator.py — 业务层：ci-fix pipeline

async def _run_ci_fix_pipeline(self, webhook: CiWebhook) -> None:
    """业务层：CI 失败 → agent 修复 → 提交 fix commit"""
    # 1. 业务层构造 AgentTask
    task = AgentTask(
        id=f"ci-{webhook.job_id}",
        kind="ci_fix",
        title=f"CI Failure: {webhook.job_name}",
        description=webhook.build_log,  # 失败日志作为 description
        context={
            "job_name": webhook.job_name,
            "build_url": webhook.build_url,
            "failed_step": webhook.failed_step,
            "branch": webhook.branch,
        },
        workspace_path=webhook.workspace_path,
    )

    # 2. 调用功能层（与 issue-to-PR 完全相同的调用方式）
    result = await self.run_task(task)

    # 3. 业务层后处理：提交 fix commit，回复 CI 评论
    if result.is_success:
        await self._commit_fix(task, result)
        await self._reply_ci_comment(webhook, result)
    else:
        await self._notify_ci_failure(webhook, result)
```

### 13.3 示例 C：定时代码巡检（未来业务）

```python
# orchestrator.py — 业务层：code-audit pipeline

async def _run_code_audit_pipeline(self, cron_config: AuditConfig) -> None:
    """业务层：定时触发 → 代码审查 → 生成报告"""
    task = AgentTask(
        id=f"audit-{datetime.now():%Y%m%d-%H%M}",
        kind="code_audit",
        title=f"Code Audit: {cron_config.scope}",
        description=f"Review the following areas: {cron_config.focus_areas}",
        context={
            "scope": cron_config.scope,
            "focus_areas": cron_config.focus_areas,
            "previous_audit_id": cron_config.last_audit_id,
        },
        workspace_path=cron_config.workspace_path,
    )

    result = await self.run_task(task)

    # 业务层后处理：生成审计报告，不发 PR
    await self._publish_audit_report(task, result)
```

### 13.4 拼接总结

三种业务层共享**完全相同的功能层接口**：

```
Issue-to-PR ──┐
              │
CI Auto-Fix ──┼── AgentTaskRunner.run_task(task) ──┬── AgentRunner (clawcodex)
              │                                     │
Code Audit ──┘                                     └── BackendRunner (SPI)
```

业务层差异只在：
- **输入源**：Tracker webhook vs CI webhook vs Cron
- **AgentTask 构造**：各自填充 `kind` 和 `context`
- **后处理**：git sync + PR vs commit + CI reply vs audit report

功能层**完全无感知**这些差异。

---

## 14. 改动清单汇总

| 文件 | 改动类型 | 改动量 |
|---|---|---|
| `src/orchestratord/agent_task.py` | **新增** | ~150 行 |
| `src/orchestratord/agent_task_runner.py` | **新增** | ~40 行 |
| `src/orchestratord/issue_to_task.py` | **新增** | ~80 行 |
| `src/orchestratord/prompt_builder.py` | 修改 | ~30 行（兼容 AgentTask） |
| `src/orchestratord/agent_runner.py` | 修改 | ~100 行（新增 run_task，保留 run） |
| `src/orchestratord/backend_runner.py` | 修改 | ~80 行（新增 run_task，保留 run） |
| `src/orchestratord/orchestrator.py` | 修改 | ~120 行（拆分 _run_agent_task，新增 run_task） |
| `src/orchestratord/session_state.py` | 修改 | ~5 行（task 可选字段） |
| `src/orchestratord/workflow_engine/stage_runner.py` | 修改 | ~60 行（AgentTask 替代合成 Issue） |
| `src/orchestratord/workflow_orchestrator.py` | 修改 | ~30 行（run_for_task） |
| `src/orchestratord/templates/workflow.md.template` | 修改 | ~5 行（模板语法迁移） |

**总计：约 700 行改动，其中 ~270 行是新增文件。**

---

## 15. 迁移策略

### Phase 1（本文档范围）：引入抽象层，保持兼容

1. 新增 `agent_task.py`、`agent_task_runner.py`、`issue_to_task.py`
2. `AgentRunner` 和 `BackendRunner` 新增 `run_task()` 方法
3. `PromptBuilder` 同时接受 `AgentTask` 和 `Issue`
4. `Orchestrator._run_issue()` 内部改用 `issue_to_agent_task()` + `run_task()`
5. 旧 `run()` 方法保留，标记 deprecated
6. 所有现有测试继续通过

### Phase 2（后续）：清理旧接口

1. 删除 `AgentRunner.run()` 旧签名
2. 删除 `BackendRunner.run()` 旧签名
3. `AgentSession.issue` 改为可选
4. 模板全部迁移到 `{{ task.* }}`
5. 更新测试

### Phase 3（后续）：非 Issue 场景接入

1. CI 失败自动修复：`AgentTask(kind="ci_fix")`
2. 定时代码巡检：`AgentTask(kind="code_audit")`
3. 批量重构：`AgentTask(kind="refactor")`

---

## 16. 测试策略

### 15.1 新增测试

| 测试 | 覆盖 |
|---|---|
| `test_agent_task.py` | `AgentTask.to_template_dict()`, `AgentTaskResult` 属性 |
| `test_issue_to_task.py` | `issue_to_agent_task()` 所有字段映射，往返转换 |
| `test_agent_task_runner_protocol.py` | Protocol 结构正确性 |

### 15.2 现有测试

- 所有现有 `test_agent_runner.py`、`test_backend_runner.py`、`test_orchestrator.py` 测试必须继续通过
- 旧 `run()` 方法的 deprecated wrapper 确保测试不感知内部重构

---

## 17. 非目标（明确不做）

- **不修改 SPI 层**（`AgentBackend` / `AgentSession` Protocol）— 它们已经是 task-agnostic
- **不修改 backend 插件**（codex/dsh/hermes/opencode/clawcodex）
- **不修改 Tracker 适配器**（Linear/GitHub/Gitee/GitCode/local）
- **不修改 GitSyncService** — 它继续被 Layer 2 调用，只是调用方从 `_run_issue()` 的一个大方法变成了同层的 pipeline 函数
- **不引入新的依赖**
- **不引入新的配置格式** — `AgentTask` 是内存数据结构，不从 YAML 反序列化