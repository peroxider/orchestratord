"""Regression tests for ``_find_pr_fallback`` head-format matching.

Issue #21: after a GitCode PR is created, the registry ``pr_number``
stays empty because ``_find_pr_fallback`` matched the open-PR list with
an exact ``candidate_head == head_branch`` comparison.  In fork mode the
requested head is ``owner/repo:branch`` (``git/sync.py``), while GitCode
returns the head field as a bare branch name, ``owner:branch``, or
``owner/repo:branch`` — so the exact match misses and the number/url are
never backfilled.

The fix normalizes both sides to the bare branch name
(``head.split(':')[-1]``) before comparing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestratord.git.sync import GitSyncService
from orchestratord.tracker import PullRequestRef

_FORK_HEAD = "evanTang/orchestratord:feat/issue-21"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't actually sleep between fallback poll cycles.

    Without the fix the match never succeeds, so the 15-cycle × 2s retry
    loop would otherwise stall the (expected-failing) red run by 30s per
    test.  With the fix the first cycle matches and sleep is never hit.
    """

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)



def _candidate(
    head: str,
    number: str = "42",
    url: str = "https://gitcode.com/evanTang/orchestratord/pulls/42",
) -> SimpleNamespace:
    """Build a fake open-PR list item exposing a head field plus number/url."""
    return SimpleNamespace(
        head_ref=head,
        head_branch=None,
        source_branch=None,
        number=number,
        url=url,
        title="PR title",
    )


def _service(candidates: list[SimpleNamespace]) -> GitSyncService:
    """Build a service whose tracker never finds via find_pull_request."""
    tracker = AsyncMock()
    tracker.find_pull_request.return_value = None
    tracker.list_pull_requests.return_value = candidates
    return GitSyncService(tracker=tracker)


@pytest.mark.asyncio
async def test_find_pr_fallback_matches_bare_branch_head() -> None:
    """head=``branch`` backfills a fork-mode ``owner/repo:branch`` request."""
    candidate = _candidate("feat/issue-21")
    service = _service([candidate])

    result = await service._find_pr_fallback(
        PullRequestRef(),
        head_branch=_FORK_HEAD,
        base_branch="master",
    )

    assert result is candidate
    assert result.number == "42"
    assert result.url == "https://gitcode.com/evanTang/orchestratord/pulls/42"


@pytest.mark.asyncio
async def test_find_pr_fallback_matches_owner_branch_head() -> None:
    """head=``owner:branch`` backfills a fork-mode ``owner/repo:branch`` request."""
    candidate = _candidate("evanTang:feat/issue-21")
    service = _service([candidate])

    result = await service._find_pr_fallback(
        PullRequestRef(),
        head_branch=_FORK_HEAD,
        base_branch="master",
    )

    assert result is candidate
    assert result.number == "42"


@pytest.mark.asyncio
async def test_find_pr_fallback_matches_owner_repo_branch_head() -> None:
    """head=``owner/repo:branch`` (identical format) still matches."""
    candidate = _candidate("evanTang/orchestratord:feat/issue-21")
    service = _service([candidate])

    result = await service._find_pr_fallback(
        PullRequestRef(),
        head_branch=_FORK_HEAD,
        base_branch="master",
    )

    assert result is candidate
    assert result.number == "42"


@pytest.mark.asyncio
async def test_find_pr_fallback_matches_candidate_without_head_ref() -> None:
    """head exposed via ``head_branch``/``source_branch`` also normalizes."""
    candidate = SimpleNamespace(
        head_ref=None,
        head_branch="evanTang:feat/issue-21",
        source_branch=None,
        number="42",
        url="https://gitcode.com/evanTang/orchestratord/pulls/42",
        title="PR title",
    )
    service = _service([candidate])

    result = await service._find_pr_fallback(
        PullRequestRef(),
        head_branch=_FORK_HEAD,
        base_branch="master",
    )

    assert result is candidate
    assert result.number == "42"
