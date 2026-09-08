"""Tool call approval policy system.

定义归属机制域（DESIGN §6）：审批策略是 Agent 执行循环的机制组件，
与 issue→PR 业务无关。approval_policy.py（业务域壳，P6 删除）导入并
重导出以保持既有 import 路径与函数/类对象同一性。

Port of INTEGRATION.md Section 5.2 — direct Python API for tool call
interception and policy evaluation, replacing Symphony's Codex JSON-RPC.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ToolCallEvent — the event object passed to policy.evaluate()
# ---------------------------------------------------------------------------


@dataclass
class ToolCallEvent:
    """Tool call event passed to the agent loop.

    Attaches approval state so the policy can mutate it in-place
    (no need to return a separate result object).
    """

    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str | None = None
    _approved: bool | None = None
    _deny_reason: str | None = None

    def allow(self, reason: str = "") -> None:
        """Mark this tool call as approved."""
        self._approved = True
        self._deny_reason = reason if reason else None

    def deny(self, reason: str) -> None:
        """Mark this tool call as denied with a reason."""
        self._approved = False
        self._deny_reason = reason

    @property
    def is_approved(self) -> bool | None:
        """True if allowed, False if denied, None if not yet evaluated."""
        return self._approved


# ---------------------------------------------------------------------------
# ApprovalPolicy interface
# ---------------------------------------------------------------------------


class ApprovalPolicy(ABC):
    """Abstract base for tool call approval policies."""

    @abstractmethod
    def evaluate(
        self,
        event: ToolCallEvent,
        session_context: dict[str, Any],
    ) -> bool:
        """Return True to approve the tool call, False to deny.

        Implementations should call event.allow() or event.deny() to
        record the decision on the event object itself.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in policies
# ---------------------------------------------------------------------------


class NeverApprovalPolicy(ApprovalPolicy):
    """Auto-approve all tool calls — mirrors Symphony's approval_policy: never."""

    def evaluate(
        self,
        event: ToolCallEvent,
        session_context: dict[str, Any],
    ) -> bool:
        event.allow("policy=never")
        return True


class AskApprovalPolicy(ApprovalPolicy):
    """Deny all tool calls — user decision required.

    In autonomous mode this is typically not used, but provided
    for parity with Symphony's approval_policy: ask.
    """

    def evaluate(
        self,
        event: ToolCallEvent,
        session_context: dict[str, Any],
    ) -> bool:
        event.deny(reason="policy=ask (not supported in autonomous mode)")
        return False


class ApproveSafeOnlyPolicy(ApprovalPolicy):
    """Approve read-only tools (glob, grep, read, web_search, web_fetch).

    All other tools require explicit approval.
    """

    _SAFE_TOOLS: frozenset[str] = frozenset(
        {
            "glob",
            "grep",
            "read",
            "read_multiple_files",
            "web_search",
            "web_fetch",
            "toolsearch",
            "ask_user_question",
        }
    )

    def evaluate(
        self,
        event: ToolCallEvent,
        session_context: dict[str, Any],
    ) -> bool:
        if event.tool_name.lower() in self._SAFE_TOOLS:
            event.allow("policy=approve-safe-only")
            return True
        event.deny(reason=f"policy=approve-safe-only ({event.tool_name} not in safe list)")
        return False


# ---------------------------------------------------------------------------
# Policy registry
# ---------------------------------------------------------------------------


_APPROVAL_POLICY_MAP: dict[str | int, type[ApprovalPolicy]] = {
    "never": NeverApprovalPolicy,
    "ask": AskApprovalPolicy,
    "approve-safe-only": ApproveSafeOnlyPolicy,
}


def get_approval_policy(policy_name: str | dict[str, Any]) -> ApprovalPolicy:
    """Resolve policy name (or inline dict) to an ApprovalPolicy instance."""
    if isinstance(policy_name, dict):
        # Structured policies describe classes of approval prompts that must
        # be rejected.  The generic SPI event does not expose enough backend-
        # specific detail to distinguish those classes, so autonomous mode
        # must fail closed instead of silently converting a reject policy to
        # ``never`` (auto-approve).
        return AskApprovalPolicy()

    name = str(policy_name).strip().lower()
    policy_cls = _APPROVAL_POLICY_MAP.get(name)
    if policy_cls is None:
        # A misspelt policy must never widen permissions.
        logger.warning(
            "approval_policy %r is not one of %s — failing closed to 'ask'. "
            "(Typo? Fix the workflow config.)",
            policy_name,
            sorted(_APPROVAL_POLICY_MAP),
        )
        return AskApprovalPolicy()
    return policy_cls()


def resolve_approval_policy(
    sandbox_config: Any,
    agent_config: Any = None,
) -> ApprovalPolicy:
    """Resolve the effective approval policy for a daemon run.

    Explicit ``sandbox.approval_policy`` config always wins.  When the
    sandbox config is left at its structured default (i.e. the operator
    did not express an approval preference) the legacy
    ``agent.permission_mode`` is honored instead: modes whose canonical
    triple maps to ``default_decision == "allow"`` (``bypassPermissions``,
    ``auto``) auto-approve tool calls.  Without this bridge a workflow
    declaring ``permission_mode: bypassPermissions`` while leaving
    ``approval_policy`` unset silently denied every tool call.
    """
    raw = (getattr(sandbox_config, "approval_policy", None) if sandbox_config is not None else None)
    mode = str(getattr(agent_config, "permission_mode", "") or "")

    if isinstance(raw, dict):
        # A structured dict is either the SandboxConfig default (operator
        # expressed nothing) or an explicit structured reject-policy.
        from ..config.schema import SandboxConfig

        if raw == SandboxConfig().approval_policy:
            from ..config.schema import permission_mode_to_triple

            intent = permission_mode_to_triple(mode).get("default_decision", "ask")
            if intent == "allow":
                logger.info(
                    "sandbox.approval_policy unconfigured — applying "
                    "agent.permission_mode=%r (auto-approve) to tool calls",
                    mode,
                )
                return NeverApprovalPolicy()
        return AskApprovalPolicy()

    return get_approval_policy(raw if raw is not None else "never")


def build_approval_policy_map(
    sandbox_config: Any,
) -> dict[str | int, ApprovalPolicy]:
    """Build a map from policy names to instantiated policies.

    Mirrors INTEGRATION.md section 3.5 AgentRunner._approval_policy_map.
    """
    raw = getattr(sandbox_config, "approval_policy", "never") or "never"
    key = raw if isinstance(raw, str) else "inline"
    return {key: get_approval_policy(raw)}
