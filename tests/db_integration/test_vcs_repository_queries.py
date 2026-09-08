"""Lookup-vocabulary tests for the §6.5 VCS repositories (live DB).

Seeds the minimal ``installations`` / ``pull_requests`` rows each lookup
targets and asserts the method returns exactly the expected row while filtering
out others, mirroring the §6.1 repository-query suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from orchestratord.db.models import GitHubInstallation, PullRequest
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


def _now() -> datetime:
    return datetime.now(UTC)


async def test_installation_lookups(db) -> None:
    repos = Repositories(db)
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    inst = GitHubInstallation(
        id=uuid.uuid4(),
        workspace_id=ws_a,
        installation_id=12345,
        account_login="acme",
        created_at=_now(),
    )
    await repos.installations.add(inst)
    assert (await repos.installations.by_installation_id(12345)).id == inst.id
    assert await repos.installations.by_installation_id(99999) is None
    assert [i.id for i in await repos.installations.list_for_workspace(ws_a)] == [
        inst.id
    ]
    assert await repos.installations.list_for_workspace(ws_b) == []


async def test_pull_request_lookups(db) -> None:
    repos = Repositories(db)
    issue_a = uuid.uuid4()
    issue_b = uuid.uuid4()
    pr = PullRequest(
        id=uuid.uuid4(),
        issue_id=issue_a,
        repo="acme/widgets",
        number=42,
        title="Fix bug",
        state="open",
        head_sha="abc123",
        status="pending",
        created_at=_now(),
        updated_at=_now(),
    )
    await repos.pull_requests.add(pr)
    assert (
        await repos.pull_requests.by_repo_number("acme/widgets", 42)
    ).id == pr.id
    assert await repos.pull_requests.by_repo_number("acme/widgets", 43) is None
    assert [p.id for p in await repos.pull_requests.list_for_issue(issue_a)] == [
        pr.id
    ]
    assert await repos.pull_requests.list_for_issue(issue_b) == []


async def test_pull_request_upsert_is_idempotent(db) -> None:
    repos = Repositories(db)
    await repos.pull_requests.upsert(
        repo="acme/widgets", number=7, title="v1", state="open", head_sha="sha1"
    )
    await repos.pull_requests.upsert(
        repo="acme/widgets", number=7, title="v2", state="merged", head_sha="sha2"
    )
    prs = await repos.pull_requests.all()
    assert [p.repo for p in prs] == ["acme/widgets"]
    assert prs[0].number == 7
    assert prs[0].title == "v2"
    assert prs[0].state == "merged"
    assert prs[0].head_sha == "sha2"
