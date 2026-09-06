"""VCS integration REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §6.5).

GitHub-only this cycle (GitLab deferred). Three surfaces:

* **installations** — one GitHub App installation per workspace, registered
  after the OAuth / App-install handshake. The ``installation_id`` is the
  GitHub-global App-installation id; ``account_login`` is the org/user that
  installed it.
* **pull_requests** — the per-issue PR rollup the issue-detail view reads. Rows
  are written by the issue→PR application and kept current by the webhook.
* **webhook** — ``POST /api/vcs/github/webhook`` verifies the
  ``X-Hub-Signature-256`` HMAC (secret from
  ``ORCHESTRATORD_GITHUB_WEBHOOK_SECRET``; verification is skipped when unset,
  for local dev) and routes ``pull_request`` (upsert PR) / ``check_run``
  (update PR check status) / ``issues`` / ``push`` (ack) events.

The ``issues`` / ``push`` events are acknowledged but not deeply synced:
GitHub-issue↔orchestratord-issue bidirectional sync is a separate concern the
doc leaves to the issue→PR application boundary, not the webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories

router = APIRouter(tags=["vcs"])


def _webhook_secret() -> str:
    return os.environ.get("ORCHESTRATORD_GITHUB_WEBHOOK_SECRET", "")


def _installation_payload(inst: orm.GitHubInstallation) -> dict:
    return {
        "id": str(inst.id),
        "workspace_id": str(inst.workspace_id),
        "installation_id": inst.installation_id,
        "account_login": inst.account_login,
        "created_at": inst.created_at.isoformat(),
    }


def _pr_payload(pr: orm.PullRequest) -> dict:
    return {
        "id": str(pr.id),
        "issue_id": str(pr.issue_id) if pr.issue_id else None,
        "repo": pr.repo,
        "number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "head_sha": pr.head_sha,
        "status": pr.status,
        "created_at": pr.created_at.isoformat(),
        "updated_at": pr.updated_at.isoformat(),
    }


class _InstallationCreate(BaseModel):
    installation_id: int
    account_login: str


@router.post(
    "/api/workspaces/{workspace_id}/vcs/installations", status_code=201
)
async def register_installation(
    workspace_id: UUID,
    body: _InstallationCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    existing = await repos.installations.by_installation_id(body.installation_id)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="installation already registered"
        )
    inst = await repos.installations.add(
        orm.GitHubInstallation(
            id=uuid4(),
            workspace_id=workspace_id,
            installation_id=body.installation_id,
            account_login=body.account_login,
            created_at=datetime.now(UTC),
        )
    )
    return _installation_payload(inst)


@router.get("/api/workspaces/{workspace_id}/vcs/installations")
async def list_installations(
    workspace_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    rows = await repos.installations.list_for_workspace(workspace_id)
    return {"installations": [_installation_payload(i) for i in rows]}


@router.get("/api/issues/{issue_id}/pull-requests")
async def list_issue_pull_requests(
    issue_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    if await repos.issues.get(issue_id) is None:
        raise HTTPException(status_code=404, detail="issue not found")
    rows = await repos.pull_requests.list_for_issue(issue_id)
    return {"pull_requests": [_pr_payload(p) for p in rows]}


def _verify_signature(body: bytes, signature: str | None) -> bool:
    secret = _webhook_secret()
    if not secret:
        return True
    if signature is None or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def _pr_state_from_event(payload: dict) -> str:
    pr = payload.get("pull_request", {})
    state = pr.get("state", "open")
    if state == "closed":
        return "merged" if pr.get("merged") else "closed"
    return state


async def _handle_pull_request(payload: dict, repos: Repositories) -> None:
    repo = payload.get("repository", {}).get("full_name", "")
    pr_payload = payload.get("pull_request", {})
    number = pr_payload.get("number")
    if not repo or number is None:
        return
    await repos.pull_requests.upsert(
        repo=repo,
        number=number,
        title=pr_payload.get("title", ""),
        state=_pr_state_from_event(payload),
        head_sha=pr_payload.get("head", {}).get("sha", ""),
    )


async def _handle_check_run(payload: dict, repos: Repositories) -> None:
    repo = payload.get("repository", {}).get("full_name", "")
    check_run = payload.get("check_run", {})
    if check_run.get("status") == "completed":
        status = (
            "success" if check_run.get("conclusion") == "success" else "failure"
        )
    else:
        status = "pending"
    now = datetime.now(UTC)
    for ref in check_run.get("pull_requests", []):
        number = ref.get("number")
        if not repo or number is None:
            continue
        pr = await repos.pull_requests.by_repo_number(repo, number)
        if pr is not None:
            pr.status = status
            pr.updated_at = now


@router.post("/api/vcs/github/webhook")
async def github_webhook(
    request: Request,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    body = await request.body()
    if not _verify_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="invalid signature")
    event = request.headers.get("X-GitHub-Event", "")
    if not event:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Event")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    if event == "pull_request":
        await _handle_pull_request(payload, repos)
    elif event == "check_run":
        await _handle_check_run(payload, repos)
    # "issues" / "push" (and any other event) are acknowledged without a deep
    # sync — see the module docstring.

    return {"ok": True, "event": event}
