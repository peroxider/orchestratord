"""Review-feedback tracking mutations (split from issue_registry.py)."""

from __future__ import annotations

import time
from collections.abc import Mapping

from .models import IssueRecord


class FeedbackMixin:
    """Review-feedback bookkeeping on issue records.

    The host class provides ``_records`` and ``_save`` (from StorageMixin).
    """

    def mark_feedback_pending(
        self,
        issue_id: str,
        feedback_ids: list[str],
        *,
        cursor: str | None = None,
        feedback_urls: Mapping[str, str] | None = None,
    ) -> IssueRecord | None:
        """Record newly discovered feedback ids as pending.

        Skips already-processed ids, deduplicates against the pending
        set, stores canonical URLs when provided, and starts the
        staleness clock when the pending set was previously empty.
        """
        record = self._records.get(issue_id)
        if record is None:
            return None
        if not record.pending_feedback_ids:
            record.pending_feedback_since = time.time()
        seen = set(record.pending_feedback_ids)
        processed = set(record.processed_feedback_ids)
        for feedback_id in feedback_ids:
            if feedback_id in processed:
                continue
            if feedback_id not in seen:
                record.pending_feedback_ids.append(feedback_id)
                seen.add(feedback_id)
            # A feedback item may first be discovered without ``html_url``
            # and receive a reconstructed URL on a later poll. Update the
            # lookup even when the pending ID already exists.
            url = feedback_urls.get(feedback_id) if feedback_urls else None
            if url:
                record.pending_feedback_urls[feedback_id] = url
        if cursor is not None:
            record.feedback_cursor = cursor
        record.last_feedback_checked_at = time.time()
        record.touch()
        self._save()
        return record

    def mark_feedback_processed(
        self,
        issue_id: str,
        feedback_ids: list[str],
        *,
        commit_sha: str | None = None,
        cursor: str | None = None,
    ) -> IssueRecord | None:
        """Move feedback ids from pending to processed.

        Removes their URL lookups, clears the staleness clock once
        nothing remains pending, and records the follow-up commit sha /
        cursor when provided.
        """
        record = self._records.get(issue_id)
        if record is None:
            return None
        processed = set(record.processed_feedback_ids)
        for feedback_id in feedback_ids:
            if feedback_id not in processed:
                record.processed_feedback_ids.append(feedback_id)
                processed.add(feedback_id)
        record.pending_feedback_ids = [
            feedback_id
            for feedback_id in record.pending_feedback_ids
            if feedback_id not in processed
        ]
        for feedback_id in feedback_ids:
            record.pending_feedback_urls.pop(feedback_id, None)
        if not record.pending_feedback_ids:
            record.pending_feedback_since = None
        if commit_sha is not None:
            record.last_followup_commit_sha = commit_sha
        if cursor is not None:
            record.feedback_cursor = cursor
        record.last_feedback_checked_at = time.time()
        record.touch()
        self._save()
        return record

    def increment_followup_attempt(self, issue_id: str) -> IssueRecord | None:
        """Increment the follow-up attempt counter for an issue."""
        record = self._records.get(issue_id)
        if record is None:
            return None
        record.followup_attempt_count += 1
        record.touch()
        self._save()
        return record

    def clear_stale_pending(self, issue_id: str, timeout_seconds: int = 600) -> int:
        """Drop pending feedback older than ``timeout_seconds``.

        Returns:
            The number of dropped ids, or ``0`` when nothing was stale.
        """
        record = self._records.get(issue_id)
        if record is None or not record.pending_feedback_ids:
            return 0
        if record.pending_feedback_since is None:
            return 0
        elapsed = time.time() - record.pending_feedback_since
        if elapsed < timeout_seconds:
            return 0
        count = len(record.pending_feedback_ids)
        record.pending_feedback_ids = []
        record.pending_feedback_urls = {}
        record.pending_feedback_since = None
        record.touch()
        self._save()
        return count

    def mark_feedback_checked(self, issue_id: str) -> IssueRecord | None:
        """Stamp ``last_feedback_checked_at`` with the current time."""
        record = self._records.get(issue_id)
        if record is None:
            return None
        record.last_feedback_checked_at = time.time()
        record.touch()
        self._save()
        return record
