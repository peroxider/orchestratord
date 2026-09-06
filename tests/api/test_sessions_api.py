"""Sessions REST API contract (§5.2.3).

Pins the execution-log / replay surface: workspace- and issue-scoped listing,
detail, the events timeline (with from/to bounds and cursor pagination),
approve/deny resolution of ``APPROVAL_REQUEST`` events, and pause/resume/stop
lifecycle transitions.

Sessions have no HTTP create endpoint — the backend runner creates them — so
these tests seed ``Session`` / ``Event`` rows directly through a helper session
(the ``db`` fixture) and then drive the read/lifecycle endpoints over HTTP.
Runs against the live ``orchestratord_test`` database and skips when Postgres
is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.spi.events import EventKind

pytestmark = pytest.mark.database


async def _seed_session(db, **overrides) -> orm.Session:
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "issue_id": None,
        "agent_id": None,
        "run_id": None,
        "mode": "single",
        "status": "running",
    }
    defaults.update(overrides)
    session = orm.Session(created_at=datetime.now(UTC), **defaults)
    await Repositories(db).sessions.add(session)
    return session


async def _seed_event(
    db, session, seq, kind=EventKind.TEXT_DELTA, **payload
) -> orm.Event:
    event = orm.Event(
        id=uuid4(),
        session_id=session.id,
        sequence=seq,
        kind=kind.value,
        payload=payload,
        run_id=None,
        issue_id=None,
        workspace_id=session.workspace_id,
        created_at=datetime.now(UTC),
    )
    await Repositories(db).events.append(event)
    return event


async def _seed_approval_request(db, session, seq=1, request_id="req-1") -> None:
    await _seed_event(
        db,
        session,
        seq,
        kind=EventKind.APPROVAL_REQUEST,
        request_id=request_id,
        call_id="call-1",
        tool_name="run",
        arguments={},
    )


class TestList:
    async def test_list_scoped_to_workspace(self, client, db) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _seed_session(db, workspace_id=ws_a)
        await db.commit()
        resp = await client.get(f"/api/workspaces/{ws_b}/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_returns_session(self, client, db) -> None:
        ws = uuid4()
        await _seed_session(db, workspace_id=ws, mode="single")
        await db.commit()
        resp = await client.get(f"/api/workspaces/{ws}/sessions")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["workspace_id"] == str(ws)
        assert body[0]["mode"] == "single"
        assert body[0]["status"] == "running"

    async def test_list_scoped_to_issue(self, client, db) -> None:
        issue = uuid4()
        await _seed_session(db, issue_id=issue)
        await db.commit()
        resp = await client.get(f"/api/issues/{issue}/sessions")
        assert resp.status_code == 200
        assert [s["issue_id"] for s in resp.json()] == [str(issue)]


class TestDetail:
    async def test_get_session(self, client, db) -> None:
        session = await _seed_session(db, mode="single", status="running")
        await db.commit()
        resp = await client.get(f"/api/sessions/{session.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(session.id)
        assert body["mode"] == "single"
        assert body["status"] == "running"
        assert "created_at" in body

    async def test_get_unknown_session_404(self, client) -> None:
        resp = await client.get(f"/api/sessions/{uuid4()}")
        assert resp.status_code == 404


class TestEvents:
    async def test_events_round_trip(self, client, db) -> None:
        session = await _seed_session(db)
        await _seed_event(db, session, 1, kind=EventKind.TEXT_DELTA, text="hi")
        await _seed_event(db, session, 2, kind=EventKind.TURN_COMPLETE)
        await db.commit()
        resp = await client.get(f"/api/sessions/{session.id}/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert [e["seq"] for e in events] == [1, 2]
        assert events[0]["kind"] == "text_delta"
        assert events[0]["payload"] == {"text": "hi"}
        assert events[1]["kind"] == "turn_complete"

    async def test_events_from_to_bounds(self, client, db) -> None:
        session = await _seed_session(db)
        for seq in (1, 2, 3):
            await _seed_event(db, session, seq)
        await db.commit()
        resp = await client.get(
            f"/api/sessions/{session.id}/events",
            params={"from_seq": 2, "to_seq": 2},
        )
        assert resp.status_code == 200
        assert [e["seq"] for e in resp.json()["events"]] == [2]

    async def test_events_cursor_pagination(self, client, db) -> None:
        session = await _seed_session(db)
        for seq in (1, 2, 3):
            await _seed_event(db, session, seq)
        await db.commit()
        first = await client.get(
            f"/api/sessions/{session.id}/events", params={"limit": 2}
        )
        body = first.json()
        assert [e["seq"] for e in body["events"]] == [1, 2]
        assert body["next_cursor"]
        second = await client.get(
            f"/api/sessions/{session.id}/events",
            params={"cursor": body["next_cursor"]},
        )
        assert [e["seq"] for e in second.json()["events"]] == [3]

    async def test_events_unknown_session_404(self, client) -> None:
        resp = await client.get(f"/api/sessions/{uuid4()}/events")
        assert resp.status_code == 404


class TestApproveDeny:
    async def test_approve_records_decision(self, client, db) -> None:
        session = await _seed_session(db)
        await _seed_approval_request(db, session, request_id="req-1")
        await db.commit()
        resp = await client.post(
            f"/api/sessions/{session.id}/approve", json={"request_id": "req-1"}
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "approved"

    async def test_deny_records_decision(self, client, db) -> None:
        session = await _seed_session(db)
        await _seed_approval_request(db, session, request_id="req-1")
        await db.commit()
        resp = await client.post(
            f"/api/sessions/{session.id}/deny", json={"request_id": "req-1"}
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "denied"

    async def test_approve_unknown_request_404(self, client, db) -> None:
        session = await _seed_session(db)
        await _seed_approval_request(db, session, request_id="req-1")
        await db.commit()
        resp = await client.post(
            f"/api/sessions/{session.id}/approve", json={"request_id": "nope"}
        )
        assert resp.status_code == 404

    async def test_approve_unknown_session_404(self, client) -> None:
        resp = await client.post(
            f"/api/sessions/{uuid4()}/approve", json={"request_id": "req-1"}
        )
        assert resp.status_code == 404


class TestLifecycle:
    async def test_pause_resume_stop_round_trip(self, client, db) -> None:
        session = await _seed_session(db, status="running")
        await db.commit()

        paused = await client.post(f"/api/sessions/{session.id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"

        resumed = await client.post(f"/api/sessions/{session.id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "running"

        stopped = await client.post(f"/api/sessions/{session.id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"

    async def test_pause_requires_running(self, client, db) -> None:
        session = await _seed_session(db, status="stopped")
        await db.commit()
        resp = await client.post(f"/api/sessions/{session.id}/pause")
        assert resp.status_code == 409

    async def test_resume_requires_paused(self, client, db) -> None:
        session = await _seed_session(db, status="running")
        await db.commit()
        resp = await client.post(f"/api/sessions/{session.id}/resume")
        assert resp.status_code == 409

    async def test_stop_requires_active(self, client, db) -> None:
        session = await _seed_session(db, status="stopped")
        await db.commit()
        resp = await client.post(f"/api/sessions/{session.id}/stop")
        assert resp.status_code == 409
