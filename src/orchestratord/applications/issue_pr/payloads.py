"""IM 事件载荷装饰器 — issue→PR 应用的业务载荷构造（DESIGN §4.2）。

``_issue_payload`` / ``_session_payload`` / ``_issue_payload_for_task_id`` /
``_repo_label`` 自宿主下沉至应用侧：机制侧发出通用 SessionEvent，业务侧
据此装饰 title / branch / repo / pr / commit / verification 等 issue 维度
上下文。模块顶层只 import 共享基础设施，不 import orchestrator。
"""

from __future__ import annotations

from typing import Any


def repo_label(tracker: Any) -> str:
    """Build a 'owner/repo' label from the tracker, or '' if unavailable."""
    if tracker is None:
        return ""
    owner = getattr(tracker, "owner", None)
    repo = getattr(tracker, "repo", None)
    if owner and repo:
        return f"{owner}/{repo}"
    return ""


def issue_payload(tracker: Any, issue: Any, **extra: Any) -> dict[str, Any]:
    """Build a rich payload dict for IM events from an Issue + extras.

    Centralizes the issue title / branch / repo context so every emit
    call site gets consistent enrichment without repeating field
    extraction. ``extra`` kwargs are merged in (e.g. commit=, pr=,
    verification=, attempts=).
    """
    payload: dict[str, Any] = {}
    title = getattr(issue, "title", None)
    if title:
        payload["title"] = title
    branch = getattr(issue, "branch_name", None)
    if branch:
        payload["branch"] = branch
    repo = repo_label(tracker)
    if repo:
        payload["repo"] = repo
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def session_payload(
    tracker: Any, registry: Any, session: Any, **extra: Any
) -> dict[str, Any]:
    """Build a rich payload from an AgentSession + extras.

    Reads issue title/branch, repo, verification status, PR url, and
    commit sha from the session/registry, then merges ``extra``.
    """
    issue = getattr(session, "issue", None)
    payload: dict[str, Any] = {}
    if issue is not None:
        title = getattr(issue, "title", None)
        if title:
            payload["title"] = title
        branch = getattr(issue, "branch_name", None)
        if branch:
            payload["branch"] = branch
        pr_url = getattr(issue, "pr_url", None)
        if pr_url:
            payload["pr"] = pr_url
    repo = repo_label(tracker)
    if repo:
        payload["repo"] = repo
    ver = getattr(session, "verification_status", None)
    if ver:
        payload["verification"] = ver
    # Try to get commit sha from the registry record
    issue_id = getattr(issue, "id", None) if issue is not None else None
    if issue_id and registry is not None:
        record = registry.get(issue_id)
        if record is not None:
            commit = getattr(record, "commit_sha", None)
            if commit:
                payload.setdefault("commit", commit)
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def issue_payload_for_task_id(
    tracker: Any, registry: Any, task_id: str
) -> dict[str, Any]:
    """Build a payload for issue.started when only the task_id is known.

    At sink-build time the Issue object is on ``session.issue`` but
    ``_build_session_sink`` receives only the task_id. We look up the
    registry record for branch/identifier, and the tracker for repo.
    """
    payload: dict[str, Any] = {}
    record = registry.get(task_id) if registry and task_id else None
    if record is not None:
        if getattr(record, "issue_identifier", None):
            payload["title"] = record.issue_identifier
        if getattr(record, "branch_name", None):
            payload["branch"] = record.branch_name
    repo = repo_label(tracker)
    if repo:
        payload["repo"] = repo
    return payload


__all__ = [
    "repo_label",
    "issue_payload",
    "session_payload",
    "issue_payload_for_task_id",
]
