"""No redundant PATCH against GitCode when the issue is already
in the target state.

GitCode rejects PATCH bodies whose only field is ``state_event`` with
400 "at least one parameter must be provided" — a retry/reset syncing
``open`` onto an already-open issue therefore produced a 400 warning
on every retry. The adapter now skips the PATCH entirely when the
pre-read shows the issue is already in the target state.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _platform(auth_mode: str = "token"):
    from orchestratord.repo_tracker.client import RepositoryPlatform

    return RepositoryPlatform(
        name="gitcode",
        default_endpoint="https://gitcode.com/api/v5",
        auth_mode=auth_mode,
        open_state="open",
        closed_state="closed",
    )


class _FakeClient:
    """Records update_issue calls; returns a scripted pre-read."""

    def __init__(self, current_state: str | None) -> None:
        self._current_state = current_state
        self.update_calls: list[tuple[str, dict]] = []

    async def fetch_issue_states_by_ids(self, ids, **kwargs):
        if self._current_state is None:
            return []
        # ``title`` is required: Weng's update_issue_state forwards the
        # current title on genuine state transitions, so the pre-read
        # shape must carry it.
        return [
            SimpleNamespace(
                id=ids[0], state=self._current_state, labels=[], title="t"
            )
        ]


def _adapter(fake: _FakeClient):
    from orchestratord.repo_tracker.adapter import RepositoryTrackerAdapter

    platform = _platform()
    adapter = RepositoryTrackerAdapter.__new__(RepositoryTrackerAdapter)
    adapter.client = fake
    adapter.platform = platform
    adapter.active_states = ["open"]
    adapter.terminal_states = ["closed"]
    return adapter


@pytest.mark.asyncio
async def test_reopen_on_open_issue_skips_patch() -> None:
    fake = _FakeClient(current_state="open")
    adapter = _adapter(fake)

    await adapter.update_issue_state("1", "open")

    assert fake.update_calls == [], (
        "re-opening an already-open issue must not issue a PATCH "
        "(GitCode rejects state_event-only bodies with 400)"
    )


@pytest.mark.asyncio
async def test_close_on_open_issue_still_patches() -> None:
    fake = _FakeClient(current_state="open")
    adapter = _adapter(fake)

    async def _record(issue_id, **kwargs):
        fake.update_calls.append((issue_id, kwargs))

    fake.update_issue = _record

    await adapter.update_issue_state("1", "closed")

    assert len(fake.update_calls) == 1
