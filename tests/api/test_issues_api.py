"""Issues REST API contract (§5.2.1).

Pins the workspace-scoped issue board wire contract: create, patch
status/assignee/labels, comment, and mention. Backed by the SQLAlchemy
repository layer; tests run against the live ``orchestratord_test`` database
(per-test truncation) and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.1.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.agent import CORE_CAPABILITY_BITS

pytestmark = pytest.mark.database


async def _create(client, ws: str, **overrides) -> dict:
    payload = {"title": "wire up dashboard"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/issues", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_agent(client, ws: str, name: str = "codex") -> dict:
    payload = {
        "name": name,
        "provider": "codex",
        "runtime_id": str(uuid4()),
        "capabilities_cache_jsonb": {bit: True for bit in CORE_CAPABILITY_BITS},
    }
    resp = await client.post(f"/api/workspaces/{ws}/agents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_pending_issue(self, client) -> None:
        ws = str(uuid4())
        body = await _create(client, ws, title="triage backlog")
        assert body["title"] == "triage backlog"
        assert body["status"] == "pending"
        assert body["workspace_id"] == ws
        assert body["id"]

    async def test_create_with_assignee_and_labels(self, client) -> None:
        ws = str(uuid4())
        aid = str(uuid4())
        body = await _create(
            client,
            ws,
            assignee_type="agent",
            assignee_id=aid,
            labels=["p0", "backend"],
        )
        assert body["assignee_type"] == "agent"
        assert body["assignee_id"] == aid
        assert body["labels"] == ["p0", "backend"]

    async def test_create_with_invalid_assignee_rejected(self, client) -> None:
        ws = str(uuid4())
        resp = await client.post(
            f"/api/workspaces/{ws}/issues",
            json={"title": "x", "assignee_type": "team", "assignee_id": str(uuid4())},
        )
        assert resp.status_code == 422


class TestList:
    async def test_list_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = str(uuid4()), str(uuid4())
        await _create(client, ws_a, title="only in a")
        resp = await client.get(f"/api/workspaces/{ws_b}/issues")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_filter_by_status(self, client) -> None:
        ws = str(uuid4())
        await _create(client, ws, title="done one")
        issue = await _create(client, ws, title="running one")
        await client.patch(
            f"/api/workspaces/{ws}/issues/{issue['id']}",
            json={"status": "running"},
        )
        resp = await client.get(
            f"/api/workspaces/{ws}/issues", params={"status": "running"}
        )
        assert resp.status_code == 200
        statuses = {i["status"] for i in resp.json()}
        assert statuses == {"running"}

    async def test_list_search_matches_title(self, client) -> None:
        ws = str(uuid4())
        await _create(client, ws, title="refactor auth")
        await _create(client, ws, title="ship docs")
        resp = await client.get(f"/api/workspaces/{ws}/issues", params={"q": "auth"})
        assert [i["title"] for i in resp.json()] == ["refactor auth"]


class TestDetail:
    async def test_detail_includes_comments(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/comments",
            json={
                "body": "looks good",
                "author_type": "member",
                "author_id": str(uuid4()),
            },
        )
        resp = await client.get(f"/api/workspaces/{ws}/issues/{issue['id']}")
        assert resp.status_code == 200
        assert [c["body"] for c in resp.json()["comments"]] == ["looks good"]

    async def test_unknown_issue_404(self, client) -> None:
        ws = str(uuid4())
        resp = await client.get(f"/api/workspaces/{ws}/issues/{uuid4()}")
        assert resp.status_code == 404


class TestPatch:
    async def test_patch_status(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        resp = await client.patch(
            f"/api/workspaces/{ws}/issues/{issue['id']}", json={"status": "running"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    async def test_patch_assignee_enforces_pair(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        resp = await client.patch(
            f"/api/workspaces/{ws}/issues/{issue['id']}",
            json={"assignee_type": "member"},
        )
        assert resp.status_code == 422

    async def test_patch_can_clear_assignee(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws, assignee_type="agent", assignee_id=str(uuid4()))
        resp = await client.patch(
            f"/api/workspaces/{ws}/issues/{issue['id']}",
            json={"assignee_type": None, "assignee_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["assignee_type"] is None
        assert resp.json()["assignee_id"] is None


class TestComment:
    async def test_comment_round_trips(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/comments",
            json={"body": "nice"},
        )
        assert resp.status_code == 201
        assert resp.json()["body"] == "nice"
        assert resp.json()["issue_id"] == issue["id"]

    async def test_comment_authored_by_owner(self, client) -> None:
        # Single-user mode (D9): the server derives authorship (owner member),
        # the client only supplies the body.
        ws = str(uuid4())
        issue = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/comments",
            json={"body": "x", "author_type": "system", "author_id": str(uuid4())},
        )
        assert resp.status_code == 201
        assert resp.json()["author_type"] == "member"

    async def test_comment_detects_mentions(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/comments",
            json={"body": "cc @alice and @agent-name for review"},
        )
        assert resp.status_code == 201
        assert resp.json()["mentions"] == ["alice", "agent-name"]


class TestMention:
    async def test_mention_agent(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        aid = str(uuid4())
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"agent_id": aid},
        )
        assert resp.status_code == 202
        assert resp.json()["mentioned"] is True
        assert resp.json()["agent_id"] == aid

    async def test_mention_member(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        mid = str(uuid4())
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"member_id": mid},
        )
        assert resp.status_code == 202
        assert resp.json()["member_id"] == mid

    async def test_mention_agent_starts_session(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        aid = str(uuid4())
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"agent_id": aid, "text": "please review @agent for the API"},
        )
        assert resp.status_code == 202
        body = resp.json()
        session_id = body["session_id"]
        assert body["session_status"] == "pending"
        assert body["message"] is not None
        assert body["message"]["role"] == "user"
        assert "please review" in body["message"]["content"]

        detail = await client.get(f"/api/sessions/{session_id}")
        assert detail.status_code == 200
        assert detail.json()["issue_id"] == issue["id"]
        assert detail.json()["agent_id"] == aid
        assert detail.json()["status"] == "pending"

    async def test_mention_member_starts_session_without_agent(
        self, client
    ) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        mid = str(uuid4())
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"member_id": mid},
        )
        assert resp.status_code == 202
        body = resp.json()
        session_id = body["session_id"]
        # Single-user mode (D9): member mention = self-mention, no agent bind
        assert body["message"] is None

        detail = await client.get(f"/api/sessions/{session_id}")
        assert detail.status_code == 200
        assert detail.json()["issue_id"] == issue["id"]
        assert detail.json()["agent_id"] is None

    async def test_mention_text_lands_on_chat_timeline(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        aid = str(uuid4())
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"agent_id": aid, "text": "run the linter"},
        )
        session_id = resp.json()["session_id"]
        msgs = await client.get(f"/api/sessions/{session_id}/messages")
        assert msgs.status_code == 200
        messages = msgs.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["seq"] == 0
        assert messages[0]["content"] == "run the linter"

    async def test_mention_requires_exactly_one_target(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        url = f"/api/workspaces/{ws}/issues/{issue['id']}/mention"
        assert (await client.post(url, json={})).status_code == 422
        assert (
            await client.post(
                url, json={"agent_id": str(uuid4()), "member_id": str(uuid4())}
            )
        ).status_code == 422

    async def test_mention_text_resolves_agent_handle(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        agent = await _create_agent(client, ws, name="codex")
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"text": "please @codex review"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["agent_id"] == agent["id"]

        detail = await client.get(f"/api/sessions/{body['session_id']}")
        assert detail.status_code == 200
        assert detail.json()["agent_id"] == agent["id"]

    async def test_mention_text_without_explicit_id_resolves_handle(
        self, client
    ) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        agent = await _create_agent(client, ws, name="codex")
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"text": "@codex fix the flaky test"},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["agent_id"] == agent["id"]

    async def test_mention_text_member_handle_binds_member(self, client) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        resp_member = await client.post(
            f"/api/workspaces/{ws}/members",
            json={"role": "member", "name": "alice"},
        )
        assert resp_member.status_code == 201, resp_member.text
        member = resp_member.json()

        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"text": "cc @alice please take a look"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["member_id"] == member["id"]
        assert body.get("agent_id") is None

        detail = await client.get(f"/api/sessions/{body['session_id']}")
        assert detail.status_code == 200
        assert detail.json()["agent_id"] is None

    async def test_mention_text_unresolvable_handle_rejected(
        self, client
    ) -> None:
        ws = str(uuid4())
        issue = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/issues/{issue['id']}/mention",
            json={"text": "no handle here"},
        )
        assert resp.status_code == 422
