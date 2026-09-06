"""VCS REST API contract (§6.5).

Pins the GitHub integration surface: workspace-scoped App-installation
registration/listing, the per-issue pull-request rollup read, and the webhook
endpoint's HMAC verification + ``pull_request`` / ``check_run`` / ``issues`` /
``push`` routing. Webhook payloads are signed in-test with
``X-Hub-Signature-256``; ``issues`` / ``push`` are acknowledged without a deep
sync (see the router docstring).

Runs against the live ``orchestratord_test`` database and skips when Postgres
is unreachable.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database

SECRET = "test-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _post_webhook(client, event: str, payload: dict, sign: bool = True):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body)
    return await client.post("/api/vcs/github/webhook", content=body, headers=headers)


async def _register_installation(client, ws, **overrides) -> dict:
    payload = {"installation_id": 12345, "account_login": "acme"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/vcs/installations", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestInstallations:
    async def test_register_and_list(self, client) -> None:
        ws = uuid4()
        inst = await _register_installation(client, ws)
        assert inst["installation_id"] == 12345
        assert inst["account_login"] == "acme"
        body = (await client.get(f"/api/workspaces/{ws}/vcs/installations")).json()
        assert [i["installation_id"] for i in body["installations"]] == [12345]

    async def test_duplicate_installation_409(self, client) -> None:
        ws = uuid4()
        await _register_installation(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/vcs/installations",
            json={"installation_id": 12345, "account_login": "other"},
        )
        assert resp.status_code == 409

    async def test_list_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _register_installation(client, ws_a)
        body = (await client.get(f"/api/workspaces/{ws_b}/vcs/installations")).json()
        assert body["installations"] == []


class TestPullRequests:
    async def test_unknown_issue_404(self, client) -> None:
        resp = await client.get(f"/api/issues/{uuid4()}/pull-requests")
        assert resp.status_code == 404

    async def test_list_prs_scoped_to_issue(self, client, db) -> None:
        ws = uuid4()
        issue = orm.Issue(
            id=uuid4(),
            workspace_id=ws,
            title="Bug",
            description="d",
            status="open",
            assignee_type=None,
            assignee_id=None,
            created_at=datetime.now(UTC),
        )
        pr = orm.PullRequest(
            id=uuid4(),
            issue_id=issue.id,
            repo="acme/widgets",
            number=7,
            title="Fix it",
            state="open",
            head_sha="abc",
            status="pending",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        repos = Repositories(db)
        await repos.issues.add(issue)
        await repos.pull_requests.add(pr)
        await db.commit()
        body = (await client.get(f"/api/issues/{issue.id}/pull-requests")).json()
        assert [p["number"] for p in body["pull_requests"]] == [7]
        assert body["pull_requests"][0]["repo"] == "acme/widgets"


class TestWebhook:
    async def test_pull_request_creates_pr(self, client, db, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_GITHUB_WEBHOOK_SECRET", SECRET)
        payload = {
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {
                "number": 7,
                "title": "Fix it",
                "state": "open",
                "head": {"sha": "abc"},
            },
        }
        resp = await _post_webhook(client, "pull_request", payload)
        assert resp.status_code == 200
        prs = await Repositories(db).pull_requests.all()
        assert [(p.repo, p.number, p.state) for p in prs] == [
            ("acme/widgets", 7, "open")
        ]

    async def test_pull_request_closed_merged(self, client, db, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_GITHUB_WEBHOOK_SECRET", SECRET)
        payload = {
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {
                "number": 7,
                "title": "Fix it",
                "state": "closed",
                "merged": True,
                "head": {"sha": "abc"},
            },
        }
        resp = await _post_webhook(client, "pull_request", payload)
        assert resp.status_code == 200
        prs = await Repositories(db).pull_requests.all()
        assert prs[0].state == "merged"

    async def test_check_run_updates_status(self, client, db, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_GITHUB_WEBHOOK_SECRET", SECRET)
        await _post_webhook(
            client,
            "pull_request",
            {
                "repository": {"full_name": "acme/widgets"},
                "pull_request": {
                    "number": 7,
                    "title": "Fix it",
                    "state": "open",
                    "head": {"sha": "abc"},
                },
            },
        )
        resp = await _post_webhook(
            client,
            "check_run",
            {
                "repository": {"full_name": "acme/widgets"},
                "check_run": {
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [{"number": 7}],
                },
            },
        )
        assert resp.status_code == 200
        prs = await Repositories(db).pull_requests.all()
        assert prs[0].status == "success"

    async def test_pull_request_concurrent_upsert_single_row(
        self, client, db, monkeypatch
    ) -> None:
        monkeypatch.setenv("ORCHESTRATORD_GITHUB_WEBHOOK_SECRET", SECRET)
        payload = {
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {
                "number": 7,
                "title": "Fix it",
                "state": "open",
                "head": {"sha": "abc"},
            },
        }
        responses = await asyncio.gather(
            *[_post_webhook(client, "pull_request", payload) for _ in range(20)]
        )
        assert all(r.status_code == 200 for r in responses)
        prs = await Repositories(db).pull_requests.all()
        assert [(p.repo, p.number) for p in prs] == [("acme/widgets", 7)]

    async def test_issues_and_push_ack(self, client, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_GITHUB_WEBHOOK_SECRET", SECRET)
        for event in ("issues", "push"):
            payload = {"repository": {"full_name": "acme/widgets"}}
            resp = await _post_webhook(client, event, payload)
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "event": event}

    async def test_invalid_signature_401(self, client, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_GITHUB_WEBHOOK_SECRET", SECRET)
        body = json.dumps({"repository": {}}).encode()
        resp = await client.post(
            "/api/vcs/github/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
        assert resp.status_code == 401

    async def test_missing_event_400(self, client) -> None:
        resp = await client.post(
            "/api/vcs/github/webhook", content=json.dumps({}).encode()
        )
        assert resp.status_code == 400

    async def test_invalid_json_400(self, client) -> None:
        resp = await client.post(
            "/api/vcs/github/webhook",
            content=b"not json",
            headers={"X-GitHub-Event": "push"},
        )
        assert resp.status_code == 400
