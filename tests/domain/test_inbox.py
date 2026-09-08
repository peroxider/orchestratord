"""Inbox item lifecycle invariants (§5.2.6, §6.1.1).

An inbox item pings a human (APPROVAL_REQUEST / clarification / failure).
Invariants:

* ``kind`` ∈ {approval_request, clarification, failed}.
* ``status`` lifecycle: open → assigned → resolved / dismissed; ``assign`` only
  from ``open``, ``resolve`` / ``dismiss`` are terminal.
* ``assignee_type`` ∈ {member, agent}, set together with ``assignee_id``.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.6, §6.1.1.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.inbox import InboxItem


def _item(**overrides) -> InboxItem:
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "kind": "approval_request",
        "title": "needs approval",
    }
    defaults.update(overrides)
    return InboxItem(**defaults)


class TestInboxItemFields:
    def test_required_fields_present(self) -> None:
        item = _item()
        assert item.kind == "approval_request"
        assert item.title == "needs approval"
        assert item.status == "open"

    def test_defaults(self) -> None:
        item = _item()
        assert item.issue_id is None
        assert item.session_id is None
        assert item.event_seq is None
        assert item.assignee_type is None
        assert item.assignee_id is None

    def test_created_at_is_timezone_aware(self) -> None:
        assert _item().created_at.tzinfo is not None


class TestKindWhitelist:
    def test_all_kinds_accepted(self) -> None:
        for kind in ("approval_request", "clarification", "failed"):
            assert _item(kind=kind).kind == kind

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            _item(kind="mention")


class TestAssigneePairing:
    def test_assignee_type_and_id_must_pair(self) -> None:
        with pytest.raises(ValueError, match="both set or both None"):
            _item(assignee_type="member", assignee_id=None)

    def test_invalid_assignee_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="assignee_type"):
            _item(assignee_type="bot", assignee_id=uuid4())


class TestLifecycle:
    def test_assign_sets_status_and_assignee(self) -> None:
        item = _item()
        member_id = uuid4()
        item.assign("member", member_id)
        assert item.status == "assigned"
        assert item.assignee_type == "member"
        assert item.assignee_id == member_id

    def test_assign_invalid_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="assignee_type"):
            _item().assign("bot", uuid4())

    def test_assign_non_open_rejected(self) -> None:
        item = _item()
        item.resolve()
        with pytest.raises(ValueError, match="cannot assign"):
            item.assign("member", uuid4())

    def test_resolve_is_terminal(self) -> None:
        item = _item()
        item.resolve()
        assert item.status == "resolved"
        with pytest.raises(ValueError, match="cannot resolve"):
            item.resolve()

    def test_dismiss_is_terminal(self) -> None:
        item = _item()
        item.dismiss()
        assert item.status == "dismissed"
        with pytest.raises(ValueError, match="cannot dismiss"):
            item.dismiss()

    def test_resolve_then_dismiss_rejected(self) -> None:
        item = _item()
        item.resolve()
        with pytest.raises(ValueError, match="cannot dismiss"):
            item.dismiss()
