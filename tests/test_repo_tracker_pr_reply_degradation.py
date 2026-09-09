"""Regression: inline_review reply 404 degrades to issue-level comment.

Issue #32: GitCode's ``POST /repos/{owner}/{repo}/pulls/{n}/comments/{id}/replies``
returns 404 (the reviews endpoint is missing in GitCode v5). When the reply
fails with 404:
  1. The platform capability is cached (``_inline_reply_supported = False``) so
     subsequent replies skip the failed endpoint.
  2. The reply is re-posted as an issue/PR-level comment with a
     ``Re: <file>:<line>`` prefix for location context.
  3. Non-404 errors still propagate (no silent swallowing).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestratord.repo_tracker.client import RepositoryIssueClient
from orchestratord.repo_tracker.normalizers import RepositoryTrackerError
from orchestratord.tracker import PullRequestFeedback, PullRequestRef


@pytest.fixture
def client() -> RepositoryIssueClient:
    """Build a GitCode client with a mocked HTTP transport layer."""
    c = RepositoryIssueClient(
        platform="gitcode",
        owner="test-owner",
        repo="test-repo",
        api_key="fake-token",
    )
    c._request_json = AsyncMock()  # type: ignore[method-assign]
    return c


@pytest.fixture
def inline_feedback() -> PullRequestFeedback:
    """A typical inline review feedback item with file+line location."""
    return PullRequestFeedback(
        id="inline_review:188844811",
        source="inline_review",
        body="Please fix this issue.",
        author_login="reviewer",
        file_path="src/orchestratord/foo.py",
        line=42,
        severity="warning",
        status="open",
    )


@pytest.fixture
def inline_feedback_no_location() -> PullRequestFeedback:
    """Inline feedback missing file path (should still degrade gracefully)."""
    return PullRequestFeedback(
        id="inline_review:188844812",
        source="inline_review",
        body="Another comment.",
        author_login="reviewer",
        file_path=None,
        line=None,
        severity="warning",
        status="open",
    )


@pytest.fixture
def pr_ref() -> PullRequestRef:
    return PullRequestRef(number="23", url="https://gitcode.com/test-owner/test-repo/pulls/23")


# ──────────────────────────────────────────────────────────────────────
# Red-phase tests: these fail against the unpatched code because the
# current implementation raises RepositoryTrackerError on 404 instead of
# degrading to an issue comment.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_reply_404_degrades_to_issue_comment(
    client: RepositoryIssueClient,
    inline_feedback: PullRequestFeedback,
    pr_ref: PullRequestRef,
) -> None:
    """When the inline-reply endpoint returns 404, the reply should be
    posted as an issue-level comment with a ``Re: src/orchestratord/foo.py:42``
    prefix, and the failure should be cached so subsequent replies skip the
    broken endpoint.
    """
    comment_response = {"id": "9001", "body": "Handled in follow-up."}
    client._request_json.side_effect = [  # type: ignore[method-assign]
        RepositoryTrackerError("request_failed status=404 body=Not Found"),
        comment_response,
    ]

    result = await client.reply_to_pull_request_feedback(
        pull_request=pr_ref,
        feedback=inline_feedback,
        body="Handled in follow-up.",
        issue_id="23",
    )

    # The first call should be the replies endpoint (which got 404).
    # The second call should be the issue comment endpoint with a Re: prefix.
    assert client._request_json.await_count == 2  # type: ignore[attr-defined]
    # Check the second call used the issue-comments endpoint
    second_call = client._request_json.await_args_list[1]  # type: ignore[attr-defined]
    assert second_call is not None
    args, kwargs = second_call
    assert args[0] == "POST"
    assert "/issues/23/comments" in args[1]
    assert "Re: src/orchestratord/foo.py:42" in kwargs["data"]["body"]

    # Result should be the issue-comment response
    assert result == comment_response


@pytest.mark.asyncio
async def test_inline_reply_404_caches_platform_incapability(
    client: RepositoryIssueClient,
    inline_feedback: PullRequestFeedback,
    pr_ref: PullRequestRef,
) -> None:
    """After the first 404, subsequent inline replies should skip the
    replies endpoint entirely and go straight to issue-level comments.
    """
    comment_response = {"id": "9001", "body": "Handled."}
    client._request_json.side_effect = [  # type: ignore[method-assign]
        RepositoryTrackerError("request_failed status=404 body=Not Found"),
        comment_response,
        comment_response,
    ]

    # First call: 404 → degradation
    result1 = await client.reply_to_pull_request_feedback(
        pull_request=pr_ref,
        feedback=inline_feedback,
        body="First reply.",
        issue_id="23",
    )
    assert result1 == comment_response
    assert client._request_json.await_count == 2  # type: ignore[attr-defined]

    # Second call: should go directly to issue comment (no replies endpoint)
    result2 = await client.reply_to_pull_request_feedback(
        pull_request=pr_ref,
        feedback=inline_feedback,
        body="Second reply.",
        issue_id="23",
    )
    assert result2 == comment_response
    # Only 1 more call (the issue comment), not 2 (no replies attempt)
    assert client._request_json.await_count == 3  # type: ignore[attr-defined]
    third_call = client._request_json.await_args_list[2]  # type: ignore[attr-defined]
    assert third_call is not None
    _, kwargs = third_call
    assert "/issues/23/comments" in third_call.args[1]
    assert "Re: src/orchestratord/foo.py:42" in kwargs["data"]["body"]


@pytest.mark.asyncio
async def test_inline_reply_404_degrade_no_location(
    client: RepositoryIssueClient,
    inline_feedback_no_location: PullRequestFeedback,
    pr_ref: PullRequestRef,
) -> None:
    """Inline feedback without file_path/line should still degrade, just
    without the Re: prefix.
    """
    comment_response = {"id": "9002", "body": "Done."}
    client._request_json.side_effect = [  # type: ignore[method-assign]
        RepositoryTrackerError("request_failed status=404 body=Not Found"),
        comment_response,
    ]

    result = await client.reply_to_pull_request_feedback(
        pull_request=pr_ref,
        feedback=inline_feedback_no_location,
        body="Done.",
        issue_id="23",
    )

    assert result == comment_response
    assert client._request_json.await_count == 2  # type: ignore[attr-defined]
    second_call = client._request_json.await_args_list[1]  # type: ignore[attr-defined]
    assert second_call is not None
    # No Re: prefix since there is no file path / line
    assert "Re:" not in second_call.kwargs["data"]["body"]


@pytest.mark.asyncio
async def test_inline_reply_non_404_propagates(
    client: RepositoryIssueClient,
    inline_feedback: PullRequestFeedback,
    pr_ref: PullRequestRef,
) -> None:
    """Non-404 errors from the replies endpoint should still propagate
    (not silently degraded).
    """
    client._request_json.side_effect = [  # type: ignore[method-assign]
        RepositoryTrackerError("request_failed status=403 body=Forbidden"),
    ]

    with pytest.raises(RepositoryTrackerError) as exc_info:
        await client.reply_to_pull_request_feedback(
            pull_request=pr_ref,
            feedback=inline_feedback,
            body="Should not degrade.",
            issue_id="23",
        )
    assert "status=403" in str(exc_info.value)


@pytest.mark.asyncio
async def test_inline_reply_success_path(
    client: RepositoryIssueClient,
    inline_feedback: PullRequestFeedback,
    pr_ref: PullRequestRef,
) -> None:
    """When the inline reply endpoint succeeds, no degradation occurs."""
    reply_response = {"id": "42", "body": "Reply body."}
    client._request_json.side_effect = [reply_response]  # type: ignore[method-assign]

    result = await client.reply_to_pull_request_feedback(
        pull_request=pr_ref,
        feedback=inline_feedback,
        body="Reply body.",
        issue_id="23",
    )

    assert result == reply_response
    assert client._request_json.await_count == 1  # type: ignore[attr-defined]
    first_call = client._request_json.await_args_list[0]  # type: ignore[attr-defined]
    assert first_call is not None
    # Should be the replies endpoint
    assert "/pulls/23/comments/188844811/replies" in first_call.args[1]
    # No Re: prefix in the body
    assert "Re:" not in first_call.kwargs["data"]["body"]