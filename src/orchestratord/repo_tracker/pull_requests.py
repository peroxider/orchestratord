"""Pull-request lifecycle methods for repository-backed trackers.

Split out of ``repo_tracker/client.py``: all PR-related operations
(create / find / update / close, review-feedback fetching, mergeability
probing).  Implemented as a mixin consumed by
:class:`~orchestratord.repo_tracker.client.RepositoryIssueClient`
so the client keeps a single public entry point.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from ..tracker import MergeableStatus, PullRequestFeedback, PullRequestRef
from .normalizers import (
    _PAGE_SIZE,
    _build_issue_comment_url,
    _find_pull_request_in_payload,
    _is_not_found_error,
    _normalize_ci_feedback,
    _normalize_conversation_feedback,
    _normalize_inline_feedback,
    _normalize_mergeable_status,
    _normalize_pull_request,
    _normalize_review_feedback,
    RepositoryTrackerError,
)

logger = logging.getLogger(__name__)

# How many poll cycles to wait for a just-created PR to become visible.
_PR_FIND_RETRY_ATTEMPTS = 12


class RepositoryPullRequestMixin:
    """PR lifecycle operations shared by GitHub / Gitee / GitCode.

    The mixin relies on the host client providing: ``platform``,
    ``owner``, ``repo``, ``_request_json``, ``_fetch_paginated`` and
    ``fetch_comments``.
    """

    async def find_pull_request(
        self,
        *,
        head_branch: str,
        base_branch: str,
    ) -> PullRequestRef | None:
        """Find an open pull request matching the given branches.

        Args:
            head_branch: Branch carrying the changes.
            base_branch: Branch the changes target.

        Returns:
            The matching ``PullRequestRef``, or None when not found.
        """
        params: dict[str, Any] = {
            "state": self.platform.open_state,
            "base": base_branch,
        }
        if self.platform.name == "github":
            params["head"] = f"{self.owner}:{head_branch}"
        else:
            params["head"] = head_branch

        payload = await self._request_json(
            "GET",
            f"/repos/{self.owner}/{self.repo}/pulls",
            params=params,
        )
        pr = _find_pull_request_in_payload(
            payload,
            head_branch=head_branch,
            base_branch=base_branch,
            allow_unique_unmatched=True,
        )
        if pr is not None:
            return pr

        # Some GitCode responses ignore or partially apply head/base query
        # filters. Fall back to an open-PR list and match locally.
        broad_payload = await self._request_json(
            "GET",
            f"/repos/{self.owner}/{self.repo}/pulls",
            params={
                "state": self.platform.open_state,
                "per_page": _PAGE_SIZE,
                "page": 1,
            },
        )
        return _find_pull_request_in_payload(
            broad_payload,
            head_branch=head_branch,
            base_branch=base_branch,
        )

    async def fetch_pull_request_feedback(
        self,
        *,
        pull_request: PullRequestRef,
        issue_id: str | None = None,
        include_ci_failures: bool = True,
        max_log_chars_per_check: int = 12_000,
    ) -> list[PullRequestFeedback]:
        """Fetch conversation, inline, review and (optionally) CI feedback.

        Args:
            pull_request: The pull request to inspect.
            issue_id: Optional issue ID used for conversation comments.
            include_ci_failures: Whether to include failing CI checks.
            max_log_chars_per_check: Max CI body length before truncation.

        Returns:
            The collected ``PullRequestFeedback`` items.
        """
        if pull_request.number is None:
            return []

        feedback: list[PullRequestFeedback] = []
        # NOTE: issue comments are intentionally NOT fetched as review
        # feedback — once a PR exists, review feedback is collected from the
        # PR itself only (inline comments + reviews). The originating issue's
        # comment thread (orchestrator automation comments, issue-side
        # chatter) must not trigger review follow-ups.
        for _name, fetcher in [
            ("inline", lambda: self._fetch_pull_request_inline_feedback(pull_request.number)),
            ("review", lambda: self._fetch_pull_request_review_feedback(pull_request.number)),
        ]:
            try:
                feedback.extend(await fetcher())
            except RepositoryTrackerError as exc:
                if _is_not_found_error(exc) and _name in {"inline", "review"}:
                    logger.debug(
                        "Skipping unsupported %s feedback endpoint for PR #%s: %s",
                        _name,
                        pull_request.number,
                        exc,
                    )
                    continue
                logger.warning(
                    "Failed to fetch %s feedback for PR #%s: %s",
                    _name,
                    pull_request.number,
                    exc,
                )
        if include_ci_failures:
            try:
                feedback.extend(
                    await self._fetch_pull_request_ci_feedback(
                        pull_request.number,
                        max_log_chars_per_check=max_log_chars_per_check,
                    )
                )
            except RepositoryTrackerError as exc:
                logger.warning(
                    "Failed to fetch CI feedback for PR #%s: %s",
                    pull_request.number,
                    exc,
                )
        return feedback

    async def reply_to_pull_request_feedback(
        self,
        *,
        pull_request: PullRequestRef,
        feedback: PullRequestFeedback,
        body: str,
        issue_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Post a reply to a pull request feedback item.

        Returns:
            The created reply payload, or None on a non-dict response.
        """
        if pull_request.number is None:
            return None
        if feedback.source == "inline_review" and feedback.id:
            comment_id = feedback.id.split(":", 1)[1] if ":" in feedback.id else feedback.id
            endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{pull_request.number}/comments/{comment_id}/replies"
        else:
            endpoint = (
                f"/repos/{self.owner}/{self.repo}/issues/{issue_id or pull_request.number}/comments"
            )
        payload = {"body": body}
        result = await self._request_json(
            "POST",
            endpoint,
            json=payload if self.platform.auth_mode == "bearer" else None,
            data=payload if self.platform.auth_mode != "bearer" else None,
        )
        return result if isinstance(result, dict) else None

    async def update_pull_request(
        self,
        *,
        pull_request: PullRequestRef,
        title: str | None = None,
        body: str | None = None,
    ) -> PullRequestRef | None:
        """Update a pull request's title and/or body.

        Returns:
            The updated ``PullRequestRef``, or the original reference when
            there is nothing to update, or None for a missing PR number.
        """
        if pull_request.number is None:
            return None
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if not payload:
            return pull_request
        result = await self._request_json(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/pulls/{pull_request.number}",
            json=payload if self.platform.auth_mode == "bearer" else None,
            data=payload if self.platform.auth_mode != "bearer" else None,
        )
        return _normalize_pull_request(result)

    async def list_pull_requests(
        self,
        *,
        state: str = "open",
        head: str | None = None,
    ) -> list[PullRequestRef]:
        """List pull requests for the repository, normalized via
        ``_normalize_pull_request``.

        Args:
            state: PR state filter (``"open"``, ``"closed"``, ``"all"``).
            head: Optional head branch filter.  For GitHub the value is
                  sent as ``owner:head``; for other platforms it is sent
                  as-is.

        Returns:
            The list of matching ``PullRequestRef`` objects.
        """
        params: dict[str, Any] = {
            "state": state,
            "per_page": _PAGE_SIZE,
            "page": 1,
        }
        if head is not None:
            if self.platform.name == "github":
                params["head"] = f"{self.owner}:{head}"
            else:
                params["head"] = head

        page = 1
        results: list[PullRequestRef] = []
        while True:
            params["page"] = page
            payload = await self._request_json(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls",
                params=params,
            )
            if not isinstance(payload, list):
                break
            for item in payload:
                if not isinstance(item, dict):
                    continue
                pr = _normalize_pull_request(item)
                if pr is not None:
                    results.append(pr)
            if len(payload) < _PAGE_SIZE:
                break
            page += 1
        return results

    async def close_pull_request(
        self,
        pull_request: PullRequestRef,
    ) -> bool:
        """Close a remote PR so a fresh one can be opened.

        Uses `PATCH /repos/{owner}/{repo}/pulls/{number}` with
        `{"state": "closed"}`. Compatible with GitHub, Gitee, GitCode —
        all three expose the same endpoint shape and accept the
        `state=closed` payload.

        Returns True if the API call succeeded, False on transport /
        authorization error. The caller (orchestrator) decides whether
        to surface a comment to the issue; the registry will still
        be reset even if the remote close fails (best-effort).
        """
        if pull_request.number is None:
            return False
        payload: dict[str, Any] = {"state": "closed"}
        try:
            await self._request_json(
                "PATCH",
                f"/repos/{self.owner}/{self.repo}/pulls/{pull_request.number}",
                json=payload if self.platform.auth_mode == "bearer" else None,
                data=payload if self.platform.auth_mode != "bearer" else None,
            )
            return True
        except RepositoryTrackerError as exc:
            # 422 (merged PRs cannot be closed) is acceptable: the
            # operator's intent was honored, the registry just needs
            # to be reset locally. We only treat 4xx/5xx other than
            # 422 as a hard failure.
            message = str(exc)
            if "status=422" in message:
                return True
            return False

    async def create_pull_request(
        self,
        *,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
    ) -> PullRequestRef:
        """Create a pull request and wait for it to be discoverable.

        Args:
            title: Pull request title.
            head_branch: Branch carrying the changes.
            base_branch: Branch the changes target.
            body: Pull request description.

        Returns:
            The created ``PullRequestRef``.

        Raises:
            RepositoryTrackerError: When the response is invalid.
        """
        payload = {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
        }
        body_resp = await self._request_json(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            json=payload if self.platform.auth_mode == "bearer" else None,
            data=payload if self.platform.auth_mode != "bearer" else None,
        )
        pr = _normalize_pull_request(body_resp)
        if pr is None:
            raise RepositoryTrackerError("invalid_pull_request_response")
        if not pr.number or not pr.url:
            for _ in range(_PR_FIND_RETRY_ATTEMPTS):
                found = await self.find_pull_request(
                    head_branch=head_branch,
                    base_branch=base_branch,
                )
                if found is not None and found.number and found.url:
                    return found
                await asyncio.sleep(1)
        return pr

    def _backfill_feedback_url(
        self,
        item: PullRequestFeedback,
        pr_number: str,
    ) -> PullRequestFeedback:
        """Set ``item.url`` from owner/repo/number when the API omitted it.

        GitCode's issue/PR comments endpoints don't return ``html_url``, so
        normalized conversation/inline feedback would carry no clickable
        link. We reconstruct the canonical comment permalink from the
        platform web host. Only applies to comment-anchored sources
        (``conversation`` / ``inline_review``); review summaries and CI
        runs keep whatever ``html_url``/``details_url`` the API provided.
        """
        if item.url:
            return item
        if item.source not in {"conversation", "inline_review"}:
            return item
        # ``id`` is stored as ``<source>:<raw_id>``; the raw comment id is
        # the anchor fragment.
        raw_id = item.id.split(":", 1)[1] if ":" in item.id else item.id
        url = _build_issue_comment_url(self.platform, self.owner, self.repo, pr_number, raw_id)
        if url:
            return replace(item, url=url)
        return item

    async def _fetch_pull_request_conversation_feedback(
        self,
        pr_number: str,
    ) -> list[PullRequestFeedback]:
        """Fetch normalized conversation comments for a pull request."""
        comments = await self.fetch_comments(pr_number)
        feedback = [
            feedback
            for feedback in (_normalize_conversation_feedback(comment) for comment in comments)
            if feedback is not None
        ]
        return [self._backfill_feedback_url(item, pr_number) for item in feedback]

    async def _fetch_pull_request_inline_feedback(
        self,
        pr_number: str,
    ) -> list[PullRequestFeedback]:
        """Fetch normalized inline review comments for a pull request."""
        payload = await self._fetch_paginated(
            f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/comments"
        )
        feedback = [
            feedback
            for feedback in (_normalize_inline_feedback(item) for item in payload)
            if feedback is not None
        ]
        return [self._backfill_feedback_url(item, pr_number) for item in feedback]

    async def _fetch_pull_request_review_feedback(
        self,
        pr_number: str,
    ) -> list[PullRequestFeedback]:
        """Fetch normalized review summaries for a pull request."""
        payload = await self._fetch_paginated(
            f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/reviews"
        )
        return [
            feedback
            for feedback in (_normalize_review_feedback(item) for item in payload)
            if feedback is not None
        ]

    async def _fetch_pull_request_ci_feedback(
        self,
        pr_number: str,
        *,
        max_log_chars_per_check: int,
    ) -> list[PullRequestFeedback]:
        """Fetch normalized failing CI feedback for a pull request.

        Args:
            pr_number: Pull request number.
            max_log_chars_per_check: Max body length before truncation.

        Returns:
            The normalized CI feedback for failing checks.
        """
        payload = await self._request_json(
            "GET",
            f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}",
        )
        head = payload.get("head") if isinstance(payload, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or not sha:
            return []

        checks = await self._fetch_ci_checks(sha)

        if self.platform.name == "github":
            for item in checks:
                state = str(item.get("conclusion") or "").strip().lower()
                if state not in {"failure", "failed", "error", "cancelled", "timed_out"}:
                    continue
                check_id = item.get("id")
                if not check_id:
                    continue
                try:
                    annotations = await self._fetch_check_run_annotations(str(check_id))
                    if annotations:
                        item["_annotations"] = annotations
                except RepositoryTrackerError:
                    pass

        return [
            feedback
            for feedback in (
                _normalize_ci_feedback(
                    item,
                    commit_sha=sha,
                    max_log_chars_per_check=max_log_chars_per_check,
                )
                for item in checks
            )
            if feedback is not None
        ]

    async def _fetch_ci_checks(self, sha: str) -> list[dict[str, Any]]:
        """Fetch CI check/status payloads for a commit SHA."""
        if self.platform.name == "github":
            payload = await self._request_json(
                "GET",
                f"/repos/{self.owner}/{self.repo}/commits/{sha}/check-runs",
            )
            check_runs = payload.get("check_runs") if isinstance(payload, dict) else None
            return check_runs if isinstance(check_runs, list) else []
        if not self.platform.supports_ci_statuses:
            return []
        return await self._fetch_paginated(
            f"/repos/{self.owner}/{self.repo}/commits/{sha}/statuses"
        )

    async def _fetch_check_run_annotations(self, check_run_id: str) -> list[dict[str, Any]]:
        """Fetch annotations for a GitHub check-run (file/line level errors)."""
        return await self._fetch_paginated(
            f"/repos/{self.owner}/{self.repo}/check-runs/{check_run_id}/annotations"
        )

    async def fetch_pull_request_mergeable(
        self,
        *,
        pull_request: PullRequestRef,
    ) -> MergeableStatus | None:
        """Fetch normalized mergeability report for a PR.

        Implementation: ``GET /repos/{owner}/{repo}/pulls/{n}``,
        then delegate to ``_normalize_mergeable_status``. Returns
        ``None`` on transport / 4xx errors so the daemon treats
        GitCode's missing-mergeable case as a silent no-op.
        """
        pr_number = pull_request.number
        if pr_number is None:
            return None
        try:
            payload = await self._request_json(
                "GET",
                f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}",
            )
        except RepositoryTrackerError as exc:
            logger.warning(
                "fetch_pull_request_mergeable: PR %s fetch failed: %s",
                pr_number,
                exc,
            )
            return None
        if not isinstance(payload, dict):
            return _normalize_mergeable_status(
                {},
                platform=self.platform.name,
            )
        return _normalize_mergeable_status(
            payload,
            platform=self.platform.name,
        )
