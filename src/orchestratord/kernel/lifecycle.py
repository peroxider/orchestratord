"""Run lifecycle — 机制侧运行生命周期治理（DESIGN §6 kernel/lifecycle.py）。

P4 先承载机制侧验证异常：验证失败（verification commands / 证据校验）
是运行生命周期的机制性事实（§1.2 C5），定义归属机制域；git/sync.py
（业务域）导入并重导出以保持既有 import 路径与类对象同一性。

后续：429 backoff / pause-resume / timeout 治理自 orchestrator._run_issue
内联段迁入（§5 机制职责表第 4 行 ``_run_with_lifecycle``）。
"""

from __future__ import annotations


class GitSyncError(RuntimeError):
    """Raised when post-run git sync fails."""


class VerificationFailed(GitSyncError):
    """Raised when configured verification commands fail."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output
