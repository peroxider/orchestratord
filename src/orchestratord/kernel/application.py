"""Application 协议 — 机制与业务的接缝（DESIGN §4.2）。

Kernel 只理解 WorkItem/Outcome；issue→PR 的业务生命周期策略
（模式选择、prompt 构建、结果解释、rebase/followup/retry 衍生）
由 Application 实现并在组合根装配进 Kernel。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol

from .run_context import RunContext
from .work_provider import WorkItem, WorkProvider

if TYPE_CHECKING:
    from ..agent.runner import AgentTaskRunner
    from ..agent.task import AgentTaskResult


#: 业务控制命令 handler：``(issue_id/dedup_key, extra) ->``。对应现有
#: ``_apply_control_command`` 中散落的 rebase/retry/followup/review-approve
#: 分支，P4 转正后成为业务注册表条目。
CommandHandler = Callable[[str, str], Any]

#: 业务 prompt profile（P2 起 PromptRouter 的 profile 模板串）。
PromptProfile = str


@dataclass(frozen=True)
class PreparedRun:
    """prepare_run 的产物：Kernel 据此选 runner 并执行。

    对应现有 ``_launch_issue`` 中的模式选择、prompt 渲染、workspace
    分支准备等业务装配决策。
    """

    runner_key: str | None = None
    runner_override: "AgentTaskRunner | None" = None
    prompt: str | None = None
    max_turns: int | None = None
    timeout_seconds: float | None = None
    business: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """interpret_result 的结论；Kernel 按其决定后续动作。

    - DISPOSE        本工作项终结
    - RETRY(delay)   业务重试（现有 _schedule_retry / _process_retry_queue）
    - SPAWN(item)    衍生后续工作项（review followup / rebase resolution
                     都是同一业务流派生的新 run_kind，统一建模为衍生
                     WorkItem）
    - WAIT_EXTERNAL  等外部事件（澄清等待作者回复 → 挂起）
    """

    kind: str  # dispose | retry | spawn | wait_external
    reason: str = ""
    retry_delay: float | None = None
    spawn_item: WorkItem | None = None

    @classmethod
    def dispose(cls, reason: str = "") -> "Outcome":
        return cls(kind="dispose", reason=reason)

    @classmethod
    def retry(cls, delay: float | None = None, reason: str = "") -> "Outcome":
        return cls(kind="retry", reason=reason, retry_delay=delay)

    @classmethod
    def spawn(cls, item: WorkItem, reason: str = "") -> "Outcome":
        return cls(kind="spawn", reason=reason, spawn_item=item)

    @classmethod
    def wait_external(cls, reason: str = "") -> "Outcome":
        return cls(kind="wait_external", reason=reason)


class Application(Protocol):
    """业务生命周期策略协议。"""

    name: str

    # ── 工作项来源 ──
    def work_provider(self) -> WorkProvider: ...

    # ── 执行前（业务装配）──
    async def prepare_run(
        self, item: WorkItem, ctx: RunContext
    ) -> PreparedRun | None: ...

    # ── 执行后（业务解释）──
    async def interpret_result(
        self,
        item: WorkItem,
        result: "AgentTaskResult",
        ctx: RunContext,
    ) -> Outcome: ...

    # ── 控制命令注册 ──
    def control_commands(self) -> dict[str, CommandHandler]: ...

    # ── 扩展点 ──
    def prompt_profiles(self) -> dict[str, PromptProfile]:
        """业务模板注册进 PromptRouter（kernel.prompt_core，见 §4.5）。"""
        ...

    def on_kernel_event(self, event: Any) -> Any:
        """订阅机制事件；实现可返回普通值或 awaitable。"""
        ...
