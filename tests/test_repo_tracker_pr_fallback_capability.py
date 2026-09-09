"""Regression: ``_find_pr_fallback``'s tracker capabilities must exist.

Issue #21: ``GitSyncService._find_pr_fallback`` calls
``tracker.list_pull_requests(...)`` to backfill a just-created PR's
``number``/``url`` when the create response (notably GitCode) omits them.
``RepositoryTrackerAdapter`` did not implement ``list_pull_requests``, so the
call raised ``AttributeError`` — which the fallback's
``except (TypeError, AttributeError)`` silently swallowed, meaning the whole
backfill loop never ran in production.

The unit tests in ``test_git_sync_pr_fallback.py`` passed only because they
mock the tracker with an ``AsyncMock``.  This module builds a real (unmocked)
``RepositoryTrackerAdapter`` and asserts its public method table covers every
capability the fallback depends on, so a "client has it, adapter doesn't
expose it" gap fails loudly instead of silently degrading.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from orchestratord.git.sync import GitSyncService
from orchestratord.repo_tracker.adapter import RepositoryTrackerAdapter
from orchestratord.repo_tracker.client import RepositoryIssueClient
from orchestratord.repo_tracker.normalizers import _normalize_pull_request
from orchestratord.repo_tracker.pull_requests import RepositoryPullRequestMixin
from orchestratord.tracker import PullRequestRef

_FORK_HEAD = "owner/repo:feat/issue-21"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't actually sleep between fallback poll cycles.

    A regression (e.g. head-ref not populated by the normalizer) would
    otherwise stall the 15-cycle x 2s retry loop by 30s per test before
    failing.
    """

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)


def _adapter() -> RepositoryTrackerAdapter:
    """Build a real (unmocked) repository-backed tracker adapter."""
    return RepositoryTrackerAdapter(
        platform="gitcode",
        owner="owner",
        repo="repo",
        api_key="token",
    )


def _public_async_methods(cls: type) -> set[str]:
    """Collect async method names declared by a class (including bases)."""
    methods: set[str] = set()
    for name, member in inspect.getmembers(cls):
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(member):
            methods.add(name)
    return methods


def test_adapter_exposes_every_fallback_capability() -> None:
    """The adapter must expose every method ``_find_pr_fallback`` calls.

    The fallback loop (``GitSyncService._find_pr_fallback``) invokes, on the
    *tracker* object:
      - ``find_pull_request(head_branch=..., base_branch=...)``
      - ``list_pull_requests(state=..., head=...)``

    A real adapter missing either one makes the fallback silently skip
    (``AttributeError`` is caught), leaving the registry ``pr_number``/
    ``pr_url`` empty on GitCode.  Assert the adapter's public method table
    covers both so the gap cannot reappear unnoticed.
    """
    required = {"find_pull_request", "list_pull_requests"}
    available = _public_async_methods(RepositoryTrackerAdapter)
    missing = required - available
    assert not missing, (
        f"RepositoryTrackerAdapter is missing fallback-required methods: "
        f"{sorted(missing)}"
    )


def test_adapter_list_pull_requests_delegates_to_client() -> None:
    """``list_pull_requests`` must exist on the client and be delegated.

    ``_find_pr_fallback`` calls ``tracker.list_pull_requests(state=...,
    head=...)`` with keyword arguments.  The adapter must forward them to the
    client so the client's ``GET /repos/{owner}/{repo}/pulls`` implementation
    (which normalizes each item via ``_normalize_pull_request``) is actually
    reached in production.
    """
    # Client-level implementation exists.
    assert "list_pull_requests" in _public_async_methods(RepositoryPullRequestMixin)

    adapter = _adapter()
    client = adapter.client
    assert isinstance(client, RepositoryIssueClient)

    adapter.client.list_pull_requests = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            PullRequestRef(
                number="42",
                url="https://gitcode.com/owner/repo/pulls/42",
                title="PR title",
            )
        ]
    )

    result = asyncio.run(adapter.list_pull_requests(state="open", head="feat/x"))

    adapter.client.list_pull_requests.assert_awaited_once_with(
        state="open",
        head="feat/x",
    )
    assert result[0].number == "42"


@pytest.mark.parametrize(
    ("head_payload", "expected_ref"),
    [
        # GitCode returns the head as a bare branch name, owner:branch, or
        # owner/repo:branch string.
        ("feat/issue-21", "feat/issue-21"),
        ("owner:feat/issue-21", "owner:feat/issue-21"),
        ("owner/repo:feat/issue-21", "owner/repo:feat/issue-21"),
        # GitHub returns a head object with label/ref keys.
        ({"label": "owner:feat/issue-21", "ref": "feat/issue-21"}, "feat/issue-21"),
        (None, None),
    ],
)
def test_normalize_pull_request_populates_head_ref(
    head_payload: object,
    expected_ref: str | None,
) -> None:
    """``list_pull_requests`` candidates must carry ``head_ref``.

    ``_find_pr_fallback`` reads ``candidate.head_ref`` (falling back to
    ``head_branch``/``source_branch``) to match the requested fork-mode head.
    If ``_normalize_pull_request`` drops the head field, every candidate has
    ``head_ref=None`` and the backfill silently never matches.
    """
    pr = _normalize_pull_request(
        {
            "id": 42,
            "number": 42,
            "html_url": "https://gitcode.com/owner/repo/pulls/42",
            "title": "PR title",
            "head": head_payload,
            "base": "master",
        }
    )
    assert pr is not None
    assert pr.head_ref == expected_ref


@pytest.mark.asyncio
async def test_find_pr_fallback_backfills_through_real_adapter() -> None:
    """End-to-end (HTTP-mocked only): a real adapter backfills number/url.

    Regression for the reviewer-flagged production gap: ``_find_pr_fallback``
    calls ``tracker.list_pull_requests(...)``.  The old unit tests passed only
    because they replaced the tracker with an ``AsyncMock``.  Here the tracker
    is a real ``RepositoryTrackerAdapter``; only the HTTP transport
    (``client._request_json``) is stubbed to return a GitCode-style open-PR
    list whose ``head`` is a bare branch name while the caller asked for the
    fork-mode ``owner/repo:branch`` form.  ``find_pull_request`` misses (its
    exact-match comparison sees ``feat/issue-21 != owner/repo:feat/issue-21``),
    so the fallback must go through ``list_pull_requests`` + ``head_ref``
    normalization to backfill ``number``/``url``.
    """
    adapter = _adapter()
    payload = [
        {
            "id": 42,
            "number": 42,
            "html_url": "https://gitcode.com/owner/repo/pulls/42",
            "title": "PR title",
            "head": "feat/issue-21",
            "base": "master",
        }
    ]
    adapter.client._request_json = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    service = GitSyncService(tracker=adapter)
    result = await service._find_pr_fallback(
        PullRequestRef(),
        head_branch=_FORK_HEAD,
        base_branch="master",
    )

    assert result is not None
    assert result.number == "42"
    assert result.url == "https://gitcode.com/owner/repo/pulls/42"
