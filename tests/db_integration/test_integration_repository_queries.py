"""Lookup-vocabulary tests for the §7.5 integration repository (live DB).

Seeds the minimal ``integrations`` row each lookup targets and asserts the
method returns exactly the expected row while filtering out others, mirroring
the §6.1 repository-query suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from orchestratord.db.models import Integration
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


def _now() -> datetime:
    return datetime.now(UTC)


async def test_integration_lookups(db) -> None:
    repos = Repositories(db)
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    inst = Integration(
        id=uuid.uuid4(),
        workspace_id=ws_a,
        provider="slack",
        webhook_url="https://hooks.slack.com/T/B/X",
        created_at=_now(),
    )
    await repos.integrations.add(inst)
    found = await repos.integrations.by_workspace_provider(ws_a, "slack")
    assert found.id == inst.id
    assert await repos.integrations.by_workspace_provider(ws_a, "lark") is None
    assert await repos.integrations.by_workspace_provider(ws_b, "slack") is None
    assert [i.id for i in await repos.integrations.list_for_workspace(ws_a)] == [
        inst.id
    ]
    assert await repos.integrations.list_for_workspace(ws_b) == []
