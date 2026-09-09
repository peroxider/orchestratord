"""WorkItem / WorkProvider — 机制层的工作项来源抽象（DESIGN §4.1）。

机制层不关心 "issue"，只关心"待领取的工作项"。业务侧（issue→PR 的
WorkProvider 实现）负责 tracker 轮询、依赖检查、意图解析、澄清/复现门
判定——Kernel 只决定何时调用与并发上限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..agent.task import AgentTask


@dataclass(frozen=True)
class WorkItem:
    """机制层视角的工作项：一个待执行任务 + 业务私有上下文。

    ``dedup_key`` 是幂等键（issue→PR 用 issue_id）——Kernel 据此去重
    inflight；``business`` 为业务自由载荷，Kernel 不读取不解释。
    ``task`` 允许为 None：poll() 时业务原始工作对象（如 Issue）尚未
    物化成 AgentTask（物化在 launch/prepare_run 路径内完成）。
    """

    task: AgentTask | None = None
    dedup_key: str = ""
    priority: int = 0
    business: dict[str, Any] = field(default_factory=dict)


class WorkProvider(Protocol):
    """业务向 Kernel 供数的协议。"""

    async def poll(self) -> list[WorkItem]:
        """返回当前可分发的工作项。Kernel 决定何时调用与并发上限。"""
        ...

    async def on_dispatch_rejected(self, item: WorkItem, reason: str) -> None:
        """工作项被限流/取消时的回调（业务决定重新入队或丢弃）。"""
        ...
