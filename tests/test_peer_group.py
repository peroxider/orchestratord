"""Unit tests for PR4 peer group management (D16, D26, G6/R10).

Covers multi-group membership per orch_id (D16), auto pair-group
formation being idempotent and acceptance-gated (G6/R10), the D26
decentralized rule — any current member may add/remove members and
there is no owner role — plus the Redis broadcast channel/body shape
and the converge-by-replacement ``handle_message`` path with its echo
guard.
"""

from __future__ import annotations

import json

import pytest

from orchestratord.peer.group import (
    GROUP_CHANNEL_PREFIX,
    GroupError,
    GroupManager,
    NotGroupMemberError,
    group_channel,
)


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, body: str) -> None:
        self.published.append((channel, body))


def _manager(**kwargs) -> tuple[GroupManager, _FakePublisher]:
    publisher = _FakePublisher()
    kwargs.setdefault("local_orch_id", "orch-A1")
    kwargs.setdefault("publisher", publisher)
    return GroupManager(**kwargs), publisher


# -- creation & D16 multi-group membership --


async def test_create_group_requires_members() -> None:
    mgr, _ = _manager()
    with pytest.raises(GroupError, match="at least one member"):
        await mgr.create_group(name="empty")


async def test_create_group_assigns_id_and_broadcasts() -> None:
    mgr, publisher = _manager()
    grp = await mgr.create_group(name="pair", members={"orch-A1", "orch-B2"})
    assert grp.group_id.startswith("grp-")
    assert grp.members == {"orch-A1", "orch-B2"}
    assert grp.name == "pair"
    channel, body = publisher.published[-1]
    assert channel == group_channel(grp.group_id)
    assert channel.startswith(GROUP_CHANNEL_PREFIX)
    data = json.loads(body)
    assert data["event"] == "group_created"
    assert data["members"] == ["orch-A1", "orch-B2"]
    assert data["origin"] == "orch-A1"


async def test_one_orch_can_belong_to_n_groups() -> None:
    mgr, _ = _manager()
    await mgr.create_group(name="g1", members={"orch-A1", "orch-B2"})
    await mgr.create_group(name="g2", members={"orch-A1", "orch-C3"})
    await mgr.create_group(name="g3", members={"orch-B2", "orch-C3"})
    mine = mgr.groups_of("orch-A1")
    assert {g.name for g in mine} == {"g1", "g2"}
    assert mgr.groups_of("orch-C3")  # sanity: other orch found too


async def test_group_lookup_unknown_id_raises() -> None:
    mgr, _ = _manager()
    with pytest.raises(GroupError, match="unknown group"):
        mgr.group("grp-does-not-exist")


# -- G6/R10 auto-grouping --


async def test_auto_group_needs_two_peers() -> None:
    mgr, _ = _manager()
    with pytest.raises(GroupError, match="at least two peers"):
        await mgr.auto_group("orch-A1")


async def test_auto_group_is_idempotent_for_same_pair() -> None:
    mgr, _ = _manager()
    g1 = await mgr.auto_group("orch-A1", "orch-B2")
    g2 = await mgr.auto_group("orch-B2", "orch-A1")  # order irrelevant
    assert g1.group_id == g2.group_id
    # A third call does not duplicate groups either.
    g3 = await mgr.auto_group("orch-A1", "orch-B2")
    assert g3.group_id == g1.group_id
    assert len([g for g in mgr.all_groups() if g.group_id == g1.group_id]) == 1


async def test_auto_group_name_is_sorted_pair() -> None:
    mgr, _ = _manager()
    grp = await mgr.auto_group("orch-B2", "orch-A1")
    assert grp.name == "auto-orch-A1+orch-B2"


# -- D26 decentralized membership --


async def test_add_member_requires_caller_membership() -> None:
    mgr, _ = _manager()
    grp = await mgr.create_group(name="g", members={"orch-A1", "orch-B2"})
    # D26: authorization is membership-only — an outsider cannot add.
    with pytest.raises(NotGroupMemberError):
        await mgr.add_member(grp.group_id, "orch-D4", caller="orch-outsider")
    # Any current member may invite.
    updated = await mgr.add_member(grp.group_id, "orch-C3", caller="orch-B2")
    assert "orch-C3" in updated.members


async def test_remove_member_kick_rules() -> None:
    mgr, _ = _manager()
    grp = await mgr.create_group(
        name="g", members={"orch-A1", "orch-B2", "orch-C3"}
    )
    with pytest.raises(NotGroupMemberError):
        await mgr.remove_member(grp.group_id, "orch-B2", caller="orch-outsider")
    with pytest.raises(GroupError, match="is not in group"):
        await mgr.remove_member(grp.group_id, "orch-nobody", caller="orch-A1")
    updated = await mgr.remove_member(grp.group_id, "orch-C3", caller="orch-B2")
    assert "orch-C3" not in updated.members
    assert "orch-B2" in updated.members  # kicker unaffected


async def test_leave_requires_no_caller() -> None:
    mgr, _ = _manager()
    grp = await mgr.create_group(name="g", members={"orch-A1", "orch-B2"})
    await mgr.leave(grp.group_id, "orch-B2")
    assert mgr.group(grp.group_id).members == {"orch-A1"}


async def test_membership_changes_broadcast() -> None:
    mgr, publisher = _manager()
    grp = await mgr.create_group(name="g", members={"orch-A1", "orch-B2"})
    publisher.published.clear()
    await mgr.add_member(grp.group_id, "orch-C3", caller="orch-A1")
    await mgr.remove_member(grp.group_id, "orch-B2", caller="orch-A1")
    events = [json.loads(body)["event"] for _, body in publisher.published]
    assert events == ["member_added", "member_removed"]


async def test_no_publisher_means_no_broadcast() -> None:
    mgr = GroupManager(local_orch_id="orch-A1", publisher=None)
    grp = await mgr.create_group(name="g", members={"orch-A1", "orch-B2"})
    assert grp.group_id.startswith("grp-")  # did not raise


# -- Redis convergence (handle_message) --


async def test_handle_message_creates_unknown_group() -> None:
    mgr, _ = _manager()
    body = json.dumps(
        {
            "event": "group_created",
            "group_id": "grp-remote1",
            "name": "from-B2",
            "members": ["orch-B2", "orch-C3"],
            "origin": "orch-B2",
        }
    )
    assert mgr.handle_message(f"{GROUP_CHANNEL_PREFIX}:grp-remote1", body) is True
    grp = mgr.group("grp-remote1")
    assert grp.members == {"orch-B2", "orch-C3"}
    assert grp.name == "from-B2"


async def test_handle_message_converges_existing_group() -> None:
    mgr, _ = _manager()
    grp = await mgr.create_group(name="g", members={"orch-A1", "orch-B2"})
    body = json.dumps(
        {
            "event": "member_added",
            "group_id": grp.group_id,
            "name": "g",
            "members": ["orch-A1", "orch-B2", "orch-C3"],
            "origin": "orch-B2",
        }
    )
    assert mgr.handle_message(group_channel(grp.group_id), body) is True
    assert mgr.group(grp.group_id).members == {"orch-A1", "orch-B2", "orch-C3"}


async def test_handle_message_ignores_own_echo() -> None:
    mgr, _ = _manager()
    body = json.dumps(
        {
            "event": "group_created",
            "group_id": "grp-x",
            "name": "g",
            "members": ["orch-A1", "orch-B2"],
            "origin": "orch-A1",  # our own broadcast echoed back
        }
    )
    assert mgr.handle_message(group_channel("grp-x"), body) is False
    with pytest.raises(GroupError):
        mgr.group("grp-x")


async def test_handle_message_rejects_garbage() -> None:
    mgr, _ = _manager()
    prefix = GROUP_CHANNEL_PREFIX
    assert mgr.handle_message(f"{prefix}:g", "not-json") is False
    assert mgr.handle_message(f"{prefix}:g", json.dumps(["list"])) is False
    assert mgr.handle_message(f"{prefix}:g", json.dumps({"no": "fields"})) is False
    assert (
        mgr.handle_message(
            "unrelated:channel", json.dumps({"group_id": "g", "members": []})
        )
        is False
    )
