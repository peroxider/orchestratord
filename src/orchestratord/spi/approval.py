"""SPI: ApprovalPolicy — 审批策略（Approval Callbacks）

When a backend supports approval_hooks, the orchestration core may
intercept tool calls before execution and wait for a human (or
automated policy) decision.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ApprovalDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALWAYS_ALLOW = "always_allow"
    CANCEL = "cancel"


@dataclass
class ApprovalRequest:
    """A tool call that is pending approval."""

    request_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalPolicy:
    """Per-session approval configuration.

    If None is passed to SessionSpec, the backend's default applies
    (which may be "ask every time" or "auto-approve safe tools").
    """

    auto_approve: list[str] = field(default_factory=list)
    require_approval: list[str] = field(default_factory=list)
    timeout_seconds: float = 300.0