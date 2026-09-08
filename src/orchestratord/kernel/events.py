"""KernelEvent / KernelHooks — Kernel 对外事件与装配钩子（DESIGN §4.7）。

取代 cli/server.py 中对 ``Orchestrator.run`` 的 monkey-patch：server 以
KernelHooks 实现向 Kernel 注入 IM 网关装配逻辑，Kernel 在对应时点显式
调用钩子，机制层不再被外部改写方法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from .run_context import RunContext

if TYPE_CHECKING:
    from .kernel import OrchestrationKernel
    from ..sinks.progress import CompositeProgressSink


class KernelEventKind(str, Enum):
    """机制事件种类（§4.2 on_kernel_event 的载荷类型之一）。"""

    KERNEL_STARTED = "kernel_started"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_CANCELLED = "run_cancelled"


@dataclass(frozen=True)
class KernelEvent:
    """Kernel 发出的机制事件（应用侧订阅驱动状态机同步）。"""

    kind: KernelEventKind
    dedup_key: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class KernelHooks(Protocol):
    """宿主（server/CLI）向 Kernel 注入装配逻辑的钩子协议。

    机制层在明确定义的时点调用钩子，宿主无需 monkey-patch。
    """

    async def on_kernel_start(self, kernel: "OrchestrationKernel") -> None:
        """Kernel 启动后调用（宿主完成 IM 网关等会话级装配）。"""
        ...

    async def on_session_sink_build(
        self, sink: "CompositeProgressSink", ctx: RunContext
    ) -> "CompositeProgressSink":
        """session sink 构建时调用（宿主可包装/替换 sink，如叠加 IM 推送）。"""
        ...
