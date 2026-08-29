"""Format :class:`OrchestratorEvent` → IM-friendly text.

Only actionable state is pushed; token/tool-call detail stays in
LiveView/logs/reports. Each event type has a concise template; unknown
types fall back to ``<event_type>: <message>``.
"""

from __future__ import annotations

from .types import EventLevel, OrchestratorEvent

# event_type -> emoji prefix (actionable signal)
_PREFIX = {
    EventLevel.SUCCESS: "✅",
    EventLevel.WARN: "⚠️",
    EventLevel.ERROR: "❌",
    EventLevel.INFO: "ℹ️",
}


def format_event(event: OrchestratorEvent) -> str:
    prefix = _PREFIX.get(event.level, "ℹ️")
    issue = event.issue_id or "?"
    msg = event.message.strip() if event.message else event.event_type
    handler = _TEMPLATES.get(event.event_type)
    if handler is not None:
        return handler(event, prefix, issue)
    return f"{prefix} {issue}: {msg}"


def _issue_line(event: OrchestratorEvent, prefix: str, issue: str) -> str:
    msg = event.message.strip() if event.message else event.event_type
    pr = event.payload.get("pr")
    tail = f" · PR {pr}" if pr else ""
    return f"{prefix} {issue}: {msg}{tail}"


# -- payload helpers --------------------------------------------------------


def _pr(event: OrchestratorEvent) -> str:
    pr = event.payload.get("pr")
    return f" · PR {pr}" if pr else ""


def _title(event: OrchestratorEvent) -> str:
    """Issue title line, e.g. '「ISSUE-Fix login bug」'."""
    title = event.payload.get("title")
    return f"「ISSUE-{title}」" if title else ""


def _branch(event: OrchestratorEvent) -> str:
    """Branch line, e.g. ' · branch orchestratord/AGENTSDK-15'."""
    branch = event.payload.get("branch")
    return f" · branch {branch}" if branch else ""


def _repo(event: OrchestratorEvent) -> str:
    """Repo line, e.g. ' · repo owner/repo'."""
    repo = event.payload.get("repo")
    return f" · repo {repo}" if repo else ""


def _commit(event: OrchestratorEvent) -> str:
    """Commit line, e.g. ' · commit abc1234'."""
    commit = event.payload.get("commit")
    if not commit:
        return ""
    return f" · commit {commit[:7]}" if len(commit) > 7 else f" · commit {commit}"


def _verification(event: OrchestratorEvent) -> str:
    """Verification status line, e.g. ' · 验证 passed'."""
    ver = event.payload.get("verification")
    return f" · 验证 {ver}" if ver else ""


def _attempts(event: OrchestratorEvent) -> str:
    """Attempts line, e.g. ' · 第 2 次尝试'."""
    attempts = event.payload.get("attempts")
    return f" · 第 {attempts} 次尝试" if attempts else ""


def _turns(event: OrchestratorEvent) -> str:
    """Turn count line, e.g. ' · 42 轮'."""
    turns = event.payload.get("turns")
    return f" · {turns} 轮" if turns else ""


def _url(event: OrchestratorEvent) -> str:
    """Issue URL line, e.g. ' · https://gitcode.com/owner/repo/issues/15'."""
    url = event.payload.get("url")
    return f" · {url}" if url else ""


_TEMPLATES = {
    "orchestrator.started": lambda e, p, i: f"{p} orchestratord: IM notifications enabled",
    "orchestrator.im_registered": lambda e, p, i: (
        f"{p} orchestratord: IM gateway registered"
    ),
    "orchestrator.im_reconnected": lambda e, p, i: (
        f"{p} orchestratord: IM gateway reconnected"
    ),
    "issue.detected": lambda e, p, i: f"{p} {i}: 新增 ISSUE {_title(e)}{_repo(e)}{_url(e)}",
    "issue.started": lambda e, p, i: f"{p} {i}: 任务已启动 {_title(e)}{_branch(e)}{_repo(e)}",
    "issue.completed": lambda e, p, i: (
        f"{p} {i}: 任务完成 {_title(e)}{_branch(e)}{_verification(e)}{_commit(e)}{_pr(e)}"
    ),
    "issue.failed": lambda e, p, i: (
        f"{p} {i}: 任务失败 — {e.message}{_title(e)}{_branch(e)}{_attempts(e)}{_turns(e)}{_pr(e)}"
    ),
    "issue.verification_failed": lambda e, p, i: (
        f"{p} {i}: 验证失败 — {e.message}{_branch(e)}{_pr(e)}"
    ),
    "issue.cancelled": lambda e, p, i: f"{p} {i}: 已取消{_branch(e)}",
    "issue.rate_limit_paused": lambda e, p, i: f"{p} {i}: 因限流暂停，稍后重试",
    "pr.opened": lambda e, p, i: f"{p} {i}: PR 已开启{_branch(e)}{_commit(e)}{_pr(e)}",
    "pr.pending_review_gate": lambda e, p, i: f"{p} {i}: PR 待审批{_branch(e)}{_pr(e)}",
    "pr.updated": lambda e, p, i: f"{p} {i}: PR 已更新{_branch(e)}{_commit(e)}{_pr(e)}",
    "git.push_failed": lambda e, p, i: f"{p} {i}: 推送失败 — {e.message}{_branch(e)}",
    "post_commit_failed": lambda e, p, i: (
        f"{p} {i}: 已提交但后续步骤失败，需人工介入{_branch(e)}{_commit(e)}{_pr(e)}"
    ),
    "agent.rate_limit_circuit_open": lambda e, p, i: f"{p} {i}: 触发限流熔断，已暂停",
    "agent.stagnation": lambda e, p, i: f"{p} {i}: agent 长时间无进展{_turns(e)}",
    "agent.loop_detected": lambda e, p, i: f"{p} {i}: 检测到 agent 循环{_turns(e)}",
    "agent.max_turns_exceeded": lambda e, p, i: f"{p} {i}: 达到最大轮次{_turns(e)}",
    "verification.failed": lambda e, p, i: (
        f"{p} {i}: 验证失败 — {e.message}{_branch(e)}{_commit(e)}"
    ),
    "verification.timeout": lambda e, p, i: f"{p} {i}: 验证超时{_branch(e)}",
    "clarification.notify_emitted": lambda e, p, i: f"{p} {i}: 需要澄清 — {e.message}",
    "clarification.escalated_to_author": lambda e, p, i: f"{p} {i}: 澄清升级给作者",
    "clarification.exhausted": lambda e, p, i: f"{p} {i}: 澄清次数耗尽",
    "review_feedback.pending_manual": lambda e, p, i: f"{p} {i}: PR review 待人工处理{_pr(e)}",
    "control.pause": lambda e, p, i: f"{p} {i}: paused",
    "control.resume": lambda e, p, i: f"{p} {i}: resumed",
    "control.stop": lambda e, p, i: f"{p} {i}: 已停止",
    "control.takeover": lambda e, p, i: f"{p} {i}: 运维接管",
    "intent.retry": lambda e, p, i: f"{p} {i}: 重试已触发{_attempts(e)}",
    "intent.followup": lambda e, p, i: f"{p} {i}: follow-up 已入队",
    "orchestrator.loop_error": lambda e, p, i: f"{p} {i}: 守护循环错误 — {e.message}",
}


__all__ = ["format_event"]
