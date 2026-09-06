"""Issues REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.1).

Phase 1 serves a workspace-scoped issue board over the persistence layer
(``orchestratord.db``). The wire contract here — create, patch
status/assignee/labels, comment, mention — is what the multica-style frontend
consumes, so it is pinned by tests regardless of the backing store.
``assignee`` polymorphism is enforced by the domain model, not re-derived here
(§5.2.1 / §6.1.1).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.issue import Issue, IssueComment
from orchestratord.domain.mention import parse_mentions

router = APIRouter(prefix="/api/workspaces/{workspace_id}/issues", tags=["issues"])


async def _issue_payload(
    repos: Repositories, issue: orm.Issue, labels: list[str] | None = None
) -> dict:
    if labels is None:
        label_rows = await repos.issue_labels.list_for_issue(issue.id)
        labels = [l.name for l in label_rows]
    return {
        "id": str(issue.id),
        "workspace_id": str(issue.workspace_id),
        "title": issue.title,
        "description": issue.description,
        "status": issue.status,
        "assignee_type": issue.assignee_type,
        "assignee_id": str(issue.assignee_id) if issue.assignee_id else None,
        "labels": list(labels),
        "created_at": issue.created_at.isoformat(),
    }


def _comment_payload(comment: orm.IssueComment) -> dict:
    return {
        "id": str(comment.id),
        "issue_id": str(comment.issue_id),
        "author_type": comment.author_type,
        "author_id": str(comment.author_id),
        "body": comment.body,
        "mentions": parse_mentions(comment.body),
        "created_at": comment.created_at.isoformat(),
    }


async def _issue_or_404(
    repos: Repositories, workspace_id: UUID, issue_id: UUID
) -> orm.Issue:
    issue = await repos.issues.get(issue_id)
    if issue is None or issue.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="issue not found")
    return issue


class _IssueCreate(BaseModel):
    title: str
    description: str = ""
    assignee_type: str | None = None
    assignee_id: UUID | None = None
    labels: list[str] = []


class _IssuePatch(BaseModel):
    status: str | None = None
    assignee_type: str | None = None
    assignee_id: UUID | None = None
    labels: list[str] | None = None


class _CommentCreate(BaseModel):
    body: str
    author_type: str
    author_id: UUID


class _MentionCreate(BaseModel):
    agent_id: UUID | None = None
    member_id: UUID | None = None


@router.get("")
async def list_issues(
    workspace_id: UUID,
    status: str | None = None,
    assignee_type: str | None = None,
    assignee_id: UUID | None = None,
    q: str | None = None,
    repos: Repositories = Depends(get_repositories),
) -> list[dict]:
    issues = await repos.issues.list_for_workspace(workspace_id)
    if status is not None:
        issues = [i for i in issues if i.status == status]
    if assignee_type is not None:
        issues = [i for i in issues if i.assignee_type == assignee_type]
    if assignee_id is not None:
        issues = [i for i in issues if i.assignee_id == assignee_id]
    if q:
        needle = q.lower()
        issues = [
            i
            for i in issues
            if needle in i.title.lower() or needle in i.description.lower()
        ]
    return [await _issue_payload(repos, i) for i in issues]


@router.post("", status_code=201)
async def create_issue(
    workspace_id: UUID,
    body: _IssueCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        validated = Issue(
            id=uuid4(),
            workspace_id=workspace_id,
            title=body.title.strip(),
            description=body.description,
            assignee_type=body.assignee_type,
            assignee_id=body.assignee_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    issue = orm.Issue(
        id=validated.id,
        workspace_id=workspace_id,
        title=validated.title,
        description=validated.description,
        status=validated.status,
        assignee_type=validated.assignee_type,
        assignee_id=validated.assignee_id,
        created_at=validated.created_at,
    )
    await repos.issues.add(issue)
    for name in body.labels:
        await repos.issue_labels.add(orm.IssueLabel(issue_id=issue.id, name=name))
    return await _issue_payload(repos, issue, labels=body.labels)


@router.get("/{issue_id}")
async def get_issue(
    workspace_id: UUID,
    issue_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    issue = await _issue_or_404(repos, workspace_id, issue_id)
    payload = await _issue_payload(repos, issue)
    comments = await repos.issue_comments.list_for_issue(issue_id)
    payload["comments"] = [_comment_payload(c) for c in comments]
    return payload


@router.patch("/{issue_id}")
async def patch_issue(
    workspace_id: UUID,
    issue_id: UUID,
    body: _IssuePatch,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    issue = await _issue_or_404(repos, workspace_id, issue_id)

    provided = body.model_fields_set
    try:
        updated = Issue(
            id=issue.id,
            workspace_id=issue.workspace_id,
            title=issue.title,
            description=issue.description,
            status=body.status if "status" in provided else issue.status,
            assignee_type=(
                body.assignee_type
                if "assignee_type" in provided
                else issue.assignee_type
            ),
            assignee_id=(
                body.assignee_id if "assignee_id" in provided else issue.assignee_id
            ),
            created_at=issue.created_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    issue.status = updated.status
    issue.assignee_type = updated.assignee_type
    issue.assignee_id = updated.assignee_id
    if "labels" in provided and body.labels is not None:
        for existing in await repos.issue_labels.list_for_issue(issue_id):
            await repos.issue_labels.delete(existing)
        for name in body.labels:
            await repos.issue_labels.add(
                orm.IssueLabel(issue_id=issue_id, name=name)
            )
    return await _issue_payload(repos, issue)


@router.post("/{issue_id}/comments", status_code=201)
async def add_comment(
    workspace_id: UUID,
    issue_id: UUID,
    body: _CommentCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _issue_or_404(repos, workspace_id, issue_id)
    try:
        validated = IssueComment(
            id=uuid4(),
            issue_id=issue_id,
            author_type=body.author_type,
            author_id=body.author_id,
            body=body.body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    comment = orm.IssueComment(
        id=validated.id,
        issue_id=issue_id,
        author_type=validated.author_type,
        author_id=validated.author_id,
        body=validated.body,
        created_at=validated.created_at,
    )
    await repos.issue_comments.add(comment)
    return _comment_payload(comment)


@router.post("/{issue_id}/mention", status_code=202)
async def mention(
    workspace_id: UUID,
    issue_id: UUID,
    body: _MentionCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _issue_or_404(repos, workspace_id, issue_id)
    if (body.agent_id is None) == (body.member_id is None):
        raise HTTPException(
            status_code=422,
            detail="mention requires exactly one of agent_id or member_id",
        )
    target = (
        {"agent_id": str(body.agent_id)}
        if body.agent_id is not None
        else {"member_id": str(body.member_id)}
    )
    # Phase 5 routes mention to the notification channel (§7.5); Phase 1 acks.
    return {"mentioned": True, "issue_id": str(issue_id), **target}
