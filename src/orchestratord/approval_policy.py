"""Tool call approval policy system — compatibility re-exports.

定义归属机制域 kernel/approval.py（DESIGN §6）；此处导入并重导出，
既有 import 路径与函数/类对象同一性不变（P6 删除本壳）。

Port of INTEGRATION.md Section 5.2 — direct Python API for tool call
interception and policy evaluation, replacing Symphony's Codex JSON-RPC.
"""

from __future__ import annotations

from .kernel.approval import (
    ApprovalPolicy,
    AskApprovalPolicy,
    ApproveSafeOnlyPolicy,
    NeverApprovalPolicy,
    ToolCallEvent,
    build_approval_policy_map,
    get_approval_policy,
    resolve_approval_policy,
)

__all__ = [
    "ApprovalPolicy",
    "AskApprovalPolicy",
    "ApproveSafeOnlyPolicy",
    "NeverApprovalPolicy",
    "ToolCallEvent",
    "build_approval_policy_map",
    "get_approval_policy",
    "resolve_approval_policy",
]
