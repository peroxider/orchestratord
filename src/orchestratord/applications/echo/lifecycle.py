"""Echo 生命周期 — kernel ``Application`` 协议的最小实现（P6 演练）。

prepare_run 产出 echo prompt 的 :class:`PreparedRun`；interpret_result
把机制结果记入 ``records`` 台账并返回 :class:`Outcome.dispose`——
证明协议闭环（WorkProvider → prepare_run → 执行 → interpret_result）
无需机制域任何改动。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...kernel.application import Outcome, PreparedRun

if TYPE_CHECKING:
    from ...agent.task import AgentTaskResult
    from ...kernel.run_context import RunContext
    from ...kernel.work_provider import WorkItem


class EchoLifecycle:
    """无状态 echo 业务：固定 prompt、结果台账、零控制命令。"""

    name = "echo"

    def __init__(self, host: Any | None = None) -> None:
        # 注入宿主形态兼容：Orchestrator.__init__ 会回绑 ``instance._host``。
        self._host = host
        #: interpret_result 的结果台账：{dedup_key, status, outcome_code, output}
        self.records: list[dict[str, Any]] = []

    def work_provider(self):
        from .provider import EchoWorkProvider

        return EchoWorkProvider()

    async def prepare_run(self, item: WorkItem, ctx: RunContext) -> PreparedRun:
        """业务装配：echo 直接采用任务自带的 prompt（缺省回退任务描述）。"""
        task = item.task
        prompt = getattr(task, "prompt_override", None) or ctx.task.description
        return PreparedRun(
            prompt=prompt,
            max_turns=1,
            business={"echo.dedup_key": item.dedup_key},
        )

    async def interpret_result(
        self,
        item: WorkItem,
        result: AgentTaskResult,
        ctx: RunContext,
    ) -> Outcome:
        """记录机制结果并终结（echo 无重试/衍生/等待语义）。"""
        self.records.append(
            {
                "dedup_key": item.dedup_key,
                "status": result.status,
                "outcome_code": result.outcome_code,
                "output": result.output_text,
            }
        )
        return Outcome.dispose(reason=f"echo:{result.status}")

    def control_commands(self) -> dict[str, Any]:
        """echo 无操作员控制命令。"""
        return {}

    def prompt_profiles(self) -> dict[str, str]:
        """echo 使用 kernel 通用模板，不注册业务 profile。"""
        return {}

    def on_kernel_event(self, event: Any) -> None:
        """echo 不订阅机制事件。"""


__all__ = ["EchoLifecycle"]
