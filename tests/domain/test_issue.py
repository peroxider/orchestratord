"""Issue entity invariants (§5.2.1, §6.1.1).

The load-bearing invariant is the assignee polymorphism: an issue is assigned
to either a member or an agent via ``(assignee_type, assignee_id)``. Other
invariants:

* ``assignee_type``/``assignee_id`` are set or cleared together.
* ``issue_comments`` authors are member/agent; ``issue_labels`` and
  ``issue_status_history`` link back to their issue.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.1, §6.1.1.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.issue import (
    Issue,
    IssueComment,
    IssueLabel,
    IssueStatusChange,
)


def _issue(**overrides):
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "title": "wire up dashboard",
    }
    defaults.update(overrides)
    return Issue(**defaults)


class TestIssueFields:
    """All required fields present; status has a safe default."""

    def test_required_fields_present(self) -> None:
        i = _issue()
        for key in ("id", "workspace_id", "title", "status"):
            assert getattr(i, key) is not None, f"missing {key!r}"

    def test_status_defaults_to_pending(self) -> None:
        assert _issue().status == "pending"

    def test_status_is_free_string(self) -> None:
        # §5.2.1 lists a display subset of the runtime's status vocabulary;
        # the model must not pin it to a fixed set.
        assert _issue(status="verification_failed").status == "verification_failed"


class TestAssigneePolymorphism:
    """``assignee_type`` ∈ {member, agent}; the pair is set/cleared together."""

    def test_member_and_agent_assignee_accepted(self) -> None:
        for kind in ("member", "agent"):
            i = _issue(assignee_type=kind, assignee_id=uuid4())
            assert i.assignee_type == kind

    def test_unassigned_is_none_pair(self) -> None:
        i = _issue()
        assert i.assignee_type is None
        assert i.assignee_id is None

    def test_invalid_assignee_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="assignee_type"):
            _issue(assignee_type="team", assignee_id=uuid4())

    def test_type_without_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="both set or both None"):
            _issue(assignee_type="member")

    def test_id_without_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="both set or both None"):
            _issue(assignee_id=uuid4())


class TestIssueComment:
    """Comments link to an issue; authors are member/agent."""

    def test_member_and_agent_author_accepted(self) -> None:
        iid = uuid4()
        for kind in ("member", "agent"):
            c = IssueComment(
                id=uuid4(), issue_id=iid, author_type=kind,
                author_id=uuid4(), body="looks good",
            )
            assert c.author_type == kind
            assert c.issue_id == iid

    def test_invalid_author_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="author_type"):
            IssueComment(
                id=uuid4(), issue_id=uuid4(), author_type="system",
                author_id=uuid4(), body="x",
            )


class TestIssueLabel:
    """Labels carry an issue reference and a name."""

    def test_label_round_trips(self) -> None:
        iid = uuid4()
        lbl = IssueLabel(issue_id=iid, name="p0")
        assert lbl.issue_id == iid
        assert lbl.name == "p0"


class TestIssueStatusChange:
    """Status history records from→to and is timezone-aware."""

    def test_from_to_status(self) -> None:
        iid = uuid4()
        ch = IssueStatusChange(
            id=uuid4(), issue_id=iid,
            from_status="pending", to_status="running",
        )
        assert ch.issue_id == iid
        assert ch.from_status == "pending"
        assert ch.to_status == "running"

    def test_from_status_can_be_none(self) -> None:
        ch = IssueStatusChange(
            id=uuid4(), issue_id=uuid4(), from_status=None, to_status="queued",
        )
        assert ch.from_status is None

    def test_created_at_is_timezone_aware(self) -> None:
        for entity in (
            _issue(),
            IssueComment(
                id=uuid4(), issue_id=uuid4(), author_type="member",
                author_id=uuid4(), body="x",
            ),
            IssueStatusChange(
                id=uuid4(), issue_id=uuid4(),
                from_status=None, to_status="queued",
            ),
        ):
            assert entity.created_at.tzinfo is not None
