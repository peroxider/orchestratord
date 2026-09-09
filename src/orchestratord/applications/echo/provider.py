"""Echo 应用（DESIGN §7 P6「新业务落地演练」）。

最小业务应用：WorkProvider 产出固定 AgentTask → single mode 执行 →
interpret_result 记录结果。用于证明 kernel ``WorkProvider``/``Application``
协议闭环且机制域（issue_pr/kernel）零改动即可接入。

本模块仅依赖 kernel 协议层与 agent/task——不触碰 applications/issue_pr
与 orchestratord.orchestrator。注入宿主（Orchestrator.__init__ 回绑
``_host``）时无需任何 Echo 侧配合，故不设宿主 Protocol。
"""

from __future__ import annotations

import logging

from ...agent.task import AgentTask
from ...kernel.work_provider import WorkItem

logger = logging.getLogger(__name__)


class EchoWorkProvider:
    """固定脚本工作项的 :class:`~orchestratord.kernel.work_provider.WorkProvider`。

    ``tasks`` 为预置 AgentTask 脚本（缺省 3 条 echo 任务）；每项经
    ``poll()`` 交付恰一次，``on_dispatch_rejected`` 重新入队（演示
    回调契约）。
    """

    def __init__(self, tasks: list[AgentTask] | None = None) -> None:
        if tasks is None:
            tasks = [
                AgentTask(
                    id=f"echo-{n}",
                    kind="echo",
                    title=f"Echo task {n}",
                    description=f"Reply with exactly: ECHO-{n}",
                    prompt_override=f"Reply with exactly: ECHO-{n}",
                )
                for n in (1, 2, 3)
            ]
        self._pending: list[AgentTask] = list(tasks)
        self._delivered: set[str] = set()

    async def poll(self) -> list[WorkItem]:
        """交付所有未分发过的脚本项（每项恰一次）。"""
        ready: list[WorkItem] = []
        for task in list(self._pending):
            self._delivered.add(task.id)
            ready.append(WorkItem(task=task, dedup_key=task.id))
        self._pending.clear()
        return ready

    async def on_dispatch_rejected(self, item: WorkItem, reason: str) -> None:
        """被限流/取消的工作项重新入队，等待下一轮 poll。"""
        logger.info("echo work item %s rejected (%s) — requeued", item.dedup_key, reason)
        if item.task is None:
            return
        self._delivered.discard(item.dedup_key)
        if item.task not in self._pending:
            self._pending.append(item.task)


__all__ = ["EchoWorkProvider"]
