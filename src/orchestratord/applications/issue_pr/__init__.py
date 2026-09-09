"""Issue→PR 应用包（DESIGN §6）：原 ``applications/issue_pr.py`` 空壳
展开为包。当前成员：``app``（应用类）、``prompts``（业务模板，import
即注册进 kernel PromptRouter）；provider/interpret/commands 随 P4 余量
按 Application 协议逐步展开。

注意：本包 ``__init__`` 必须保持轻量——``app``（依赖
orchestration_subsystem）经 PEP 562 ``__getattr__`` 延迟到首次属性访问
再绑定。宿主 seam（cli/server 惰性 import 应用类 + 测试经 monkeypatch
``OrchestrationSubsystem`` 换桩）依赖这一惰性绑定时序。
"""

from __future__ import annotations

# Import 即注册：业务模板必须在本包可导入时即挂进 PromptRouter
# （组合根 orchestrator/orchestration_subsystem 经由本模块触发，与旧
# business_prompts 顶层 import 的触发语义一致）。
from . import prompts as _prompts  # noqa: F401

__all__ = ["IssueToPrApplication"]


def __getattr__(name: str):
    if name == "IssueToPrApplication":
        from orchestratord.applications.issue_pr.app import IssueToPrApplication

        return IssueToPrApplication
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
