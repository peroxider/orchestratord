"""Echo 应用组合根（DESIGN §6 / §7 P6）。

与 :class:`~orchestratord.applications.issue_pr.app.IssueToPrApplication`
同款形态：import 兼容壳（``OrchestrationSubsystem`` 子类）。echo 的
业务逻辑在 :mod:`.lifecycle`（kernel Application 协议）与
:mod:`.provider`（WorkProvider 协议）中；本类只承担组合根命名与
注册表寻址（``applications/__init__`` 的 class_path 指向此处）。
"""

from __future__ import annotations

from orchestratord.orchestration_subsystem import OrchestrationSubsystem


class EchoApplication(OrchestrationSubsystem):
    """Minimal demo application proving the Kernel/Application protocol closes."""


__all__ = ["EchoApplication"]
