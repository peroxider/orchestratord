"""Generic CRUD + facade tests over the §6.1 repository layer (live DB).

Exercises the :class:`Repository` base (``add``/``get``/``all``/``count``/
``delete``) on a representative single-PK model (``workspaces``) and asserts
the :class:`Repositories` facade wires every one of the 30 repositories.
Per-repository lookup coverage lives in ``test_repository_queries.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from orchestratord.db.models import Workspace
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


def _ws(**overrides) -> Workspace:
    defaults = {
        "id": uuid.uuid4(),
        "slug": "acme",
        "name": "Acme",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Workspace(**defaults)


async def test_add_flush_and_get_by_id(db) -> None:
    repos = Repositories(db)
    ws = await repos.workspaces.add(_ws())
    await db.commit()
    got = await repos.workspaces.get(ws.id)
    assert got is not None
    assert got.slug == "acme"
    assert got.name == "Acme"


async def test_get_missing_returns_none(db) -> None:
    repos = Repositories(db)
    assert await repos.workspaces.get(uuid.uuid4()) is None


async def test_all_and_count(db) -> None:
    repos = Repositories(db)
    await repos.workspaces.add(_ws(slug="a", name="A"))
    await repos.workspaces.add(_ws(slug="b", name="B"))
    assert await repos.workspaces.count() == 2
    assert {w.slug for w in await repos.workspaces.all()} == {"a", "b"}


async def test_delete(db) -> None:
    repos = Repositories(db)
    ws = await repos.workspaces.add(_ws())
    await db.commit()
    await repos.workspaces.delete(ws)
    await db.commit()
    assert await repos.workspaces.get(ws.id) is None
    assert await repos.workspaces.count() == 0


async def test_facade_exposes_every_repository(db) -> None:
    repos = Repositories(db)
    names = [
        "workspaces",
        "members",
        "member_agent_scopes",
        "agents",
        "agent_capabilities_cache",
        "runtimes",
        "runtime_backends",
        "issues",
        "issue_comments",
        "issue_labels",
        "issue_status_history",
        "sessions",
        "runs",
        "events",
        "approvals",
        "skills",
        "skill_source_maps",
        "skill_references",
        "inbox",
        "usage_aggregates",
        "squads",
        "squad_members",
        "projects",
        "project_repos",
        "project_docs",
        "autopilots",
        "autopilot_runs",
        "audit_log",
        "auth_tokens",
        "channels",
    ]
    assert len(names) == 30
    for name in names:
        assert hasattr(repos, name), f"missing repository: {name}"
