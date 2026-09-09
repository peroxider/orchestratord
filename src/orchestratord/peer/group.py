"""Peer group management (DESIGN G6, D16, D26, AC9; §7 R10).

A *group* is a named set of orch_ids. Per D16 the granularity is the
orch_id and a single daemon may belong to N groups simultaneously.
Management is decentralized (D26): there is no owner role — any
current member may add or remove members, enforced purely by
membership, never by a caller-identity lookup.

Two peers that have both accepted each other auto-form a pair group
(G6). Per R10 only explicitly accepted peers count — auto-grouping is
invoked by the connection layer *after* the registry says ``accepted``,
never for merely reachable daemons.

Membership changes broadcast over Redis pub/sub on the channel
``orch:peer:group:{group_id}`` (a sibling of the D22 topic namespace)
so every daemon sharing the operator's Redis converges on the same
member list.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

GROUP_CHANNEL_PREFIX = "orch:peer:group"

EVENT_MEMBER_ADDED = "member_added"
EVENT_MEMBER_REMOVED = "member_removed"
EVENT_GROUP_CREATED = "group_created"


class GroupError(Exception):
    """Group operation failed."""


class NotGroupMemberError(GroupError):
    """D26 guard: the caller is not a member of the group."""


def group_channel(group_id: str) -> str:
    """Redis channel on which *group_id*'s membership changes broadcast."""
    return f"{GROUP_CHANNEL_PREFIX}:{group_id}"


@dataclass
class PeerGroup:
    group_id: str
    name: str
    members: set[str] = field(default_factory=set)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class GroupManager:
    """In-memory group registry with Redis change broadcast (D16/D26).

    ``publisher`` is any object with ``await publish(channel, body)``
    (a redis-py asyncio client, or a test double); ``None`` disables
    broadcasting (CLI/offline use).
    """

    def __init__(self, *, local_orch_id: str, publisher: Any | None = None) -> None:
        self._local_orch_id = local_orch_id
        self._publisher = publisher
        self._groups: dict[str, PeerGroup] = {}

    # -- queries --

    def group(self, group_id: str) -> PeerGroup:
        grp = self._groups.get(group_id)
        if grp is None:
            raise GroupError(f"unknown group {group_id!r}")
        return grp

    def groups_of(self, orch_id: str) -> list[PeerGroup]:
        """All groups containing *orch_id* (D16: possibly several)."""
        return [g for g in self._groups.values() if orch_id in g.members]

    def all_groups(self) -> list[PeerGroup]:
        return list(self._groups.values())

    # -- mutations --

    async def create_group(
        self, *, name: str, members: set[str] | None = None
    ) -> PeerGroup:
        member_set = set(members or set())
        if not member_set:
            raise GroupError("a group needs at least one member")
        grp = PeerGroup(
            group_id=_new_group_id(), name=name, members=member_set
        )
        self._groups[grp.group_id] = grp
        await self._broadcast(grp, EVENT_GROUP_CREATED)
        return grp

    async def auto_group(self, *orch_ids: str) -> PeerGroup:
        """Idempotent pair/cluster formation (G6/R10).

        Called by the connection layer once both peers are mutually
        accepted: if a group already contains exactly these members it
        is returned unchanged, otherwise a new group is created.
        """
        wanted = set(orch_ids)
        if len(wanted) < 2:
            raise GroupError("auto-grouping needs at least two peers")
        for grp in self._groups.values():
            if grp.members == wanted:
                return grp
        return await self.create_group(
            name=f"auto-{'+'.join(sorted(wanted))}", members=wanted
        )

    async def add_member(
        self, group_id: str, orch_id: str, *, caller: str
    ) -> PeerGroup:
        """D26: *caller* must already be a member — that is the whole
        authorization check; no owner/admin role exists."""
        grp = self.group(group_id)
        self._require_member(grp, caller)
        grp.members.add(orch_id)
        await self._broadcast(grp, EVENT_MEMBER_ADDED)
        return grp

    async def remove_member(
        self, group_id: str, orch_id: str, *, caller: str
    ) -> PeerGroup:
        """D26 kick: same membership-only check as add."""
        grp = self.group(group_id)
        self._require_member(grp, caller)
        if orch_id not in grp.members:
            raise GroupError(f"{orch_id!r} is not in group {group_id!r}")
        grp.members.discard(orch_id)
        await self._broadcast(grp, EVENT_MEMBER_REMOVED)
        return grp

    async def leave(self, group_id: str, orch_id: str) -> PeerGroup:
        grp = self.group(group_id)
        grp.members.discard(orch_id)
        await self._broadcast(grp, EVENT_MEMBER_REMOVED)
        return grp

    # -- Redis convergence --

    async def _broadcast(self, grp: PeerGroup, event: str) -> None:
        if self._publisher is None:
            return
        body = json.dumps(
            {
                "event": event,
                "group_id": grp.group_id,
                "name": grp.name,
                "members": sorted(grp.members),
                "origin": self._local_orch_id,
            }
        )
        await self._publisher.publish(group_channel(grp.group_id), body)

    def handle_message(self, channel: str, body: str | bytes) -> bool:
        """Apply a remote membership broadcast; True when applied.

        The broadcast carries the authoritative member list, so the
        applying side converges by replacement. Frames originating from
        this daemon are ignored (echo guard, same as the event relay).
        """
        if channel and not channel.startswith(GROUP_CHANNEL_PREFIX):
            return False
        try:
            data = json.loads(body)
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        if data.get("origin") == self._local_orch_id:
            return False
        group_id = data.get("group_id")
        members = data.get("members")
        if not isinstance(group_id, str) or not isinstance(members, list):
            return False
        grp = self._groups.get(group_id)
        if grp is None:
            grp = PeerGroup(
                group_id=group_id,
                name=str(data.get("name") or group_id),
                members={str(m) for m in members},
            )
            self._groups[group_id] = grp
        else:
            grp.members = {str(m) for m in members}
        return True

    def _require_member(self, grp: PeerGroup, caller: str) -> None:
        if caller not in grp.members:
            raise NotGroupMemberError(
                f"{caller!r} is not a member of group {grp.group_id!r}"
            )


def _new_group_id() -> str:
    return f"grp-{uuid.uuid4().hex[:12]}"
