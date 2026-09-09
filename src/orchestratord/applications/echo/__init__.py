"""Echo 应用包（DESIGN §7 P6「新业务落地演练」）。

成员：``app``（组合根应用类）、``lifecycle``（kernel Application 协议
最小实现）、``provider``（固定脚本 WorkProvider）。演练目标：协议
（WorkProvider/Application/Outcome）闭环且机制域零改动。

本包 ``__init__`` 保持轻量——``app``（依赖 orchestration_subsystem）
经 PEP 562 ``__getattr__`` 延迟到首次属性访问再绑定（与 issue_pr 包
同款 import 时序契约）。
"""

from __future__ import annotations

__all__ = ["EchoApplication"]


def __getattr__(name: str):
    if name == "EchoApplication":
        from orchestratord.applications.echo.app import EchoApplication

        return EchoApplication
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
