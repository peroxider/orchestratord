from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestratord.issue_registry.issue import Issue
from orchestratord.repo_tracker.adapter import RepositoryTrackerAdapter
from orchestratord.repo_tracker.client import RepositoryIssueClient


@pytest.mark.asyncio
async def test_gitcode_lifecycle_update_uses_documented_state_endpoint() -> None:
    client = RepositoryIssueClient(
        platform="gitcode",
        owner="owner",
        repo="repo",
        api_key="token",
    )
    client._request_json = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.update_issue(
        "66",
        state="completed",
        labels=["completed"],
        title="Finished task",
    )

    client._request_json.assert_awaited_once_with(
        "PATCH",
        "/repos/owner/issues/66",
        data={
            "repo": "repo",
            "title": "Finished task",
            "state": "close",
            "labels": "completed",
        },
    )


@pytest.mark.asyncio
async def test_repository_adapter_forwards_current_title_for_gitcode_close() -> None:
    adapter = RepositoryTrackerAdapter(
        platform="gitcode",
        owner="owner",
        repo="repo",
        api_key="token",
    )
    adapter.client.fetch_issue_states_by_ids = AsyncMock(
        return_value=[
            Issue(
                id="66",
                identifier="#66",
                title="Finished task",
                state="open",
                labels=[],
            )
        ]
    )
    adapter.client.update_issue = AsyncMock()

    await adapter.update_issue_state("66", "completed")

    adapter.client.update_issue.assert_awaited_once_with(
        "66",
        state="completed",
        title="Finished task",
        labels=["completed"],
    )
