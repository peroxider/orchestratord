"""``orchestratord peer`` subcommands — operator-facing federation CLI.

DESIGN_PEER_FEDERATION §6.1 (G7/AC10): ``peer {list,invite,accept,reject,
remove,leave,group}``. The registry verbs (``list``/``accept``/``reject``/
``remove``) operate directly on the local workspace DB through the same
:mod:`orchestratord.peer.registry` functions the HTTP API uses, so the CLI
and the daemon always see one trust state. ``invite`` is the *applying*
side of the §5 handshake: it POSTs this daemon's identity to a remote
``/api/peer/invite`` endpoint. Group verbs run through
:class:`orchestratord.peer.group.GroupManager`; per v3 D26 the membership
authorization is "caller is a member" only — there is no owner to consult.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from orchestratord.db import models as orm
from orchestratord.domain.auth_token import issue_api_token
from orchestratord.peer.card import ensure_orch_id
from orchestratord.peer.group import GroupError, GroupManager
from orchestratord.peer.registry import (
    CLIENT_KIND_V1_SUNSET,
    list_peers,
    remove_peer,
    set_peer_status,
)


def add_peer_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``peer`` subcommand and its children."""
    peer_parser = subparsers.add_parser(
        "peer",
        help="Inspect and manage peer federation state (DESIGN §5/§6.1)",
        description=(
            "Operator CLI for the peer federation: list/accept/reject/remove "
            "peers in the local registry, apply to join a remote workspace, "
            "and manage decentralized peer groups (D26 — no owner)."
        ),
    )
    peer_sub = peer_parser.add_subparsers(
        dest="peer_subcommand",
        required=True,
    )

    lst = peer_sub.add_parser("list", help="List peers of a workspace")
    lst.add_argument("--workspace-id", required=True, help="Workspace UUID")
    lst.add_argument("--status", default=None, help="pending | accepted")

    invite = peer_sub.add_parser(
        "invite", help="Apply to join a remote workspace (§5 step 2)"
    )
    invite.add_argument("--url", required=True, help="Remote daemon base URL")
    invite.add_argument(
        "--workspace-id", required=True, help="Workspace UUID to join"
    )
    invite.add_argument(
        "--name",
        default=None,
        help="Display name (default: this daemon's orch_id)",
    )

    for verb, help_text in (
        ("accept", "Accept a pending peer and print its one-time token"),
        ("reject", "Reject a pending peer (hard delete)"),
    ):
        sub = peer_sub.add_parser(verb, help=help_text)
        sub.add_argument("--peer-id", required=True, help="Peer row UUID")

    rm = peer_sub.add_parser("remove", help="Remove an accepted peer (AC8)")
    rm.add_argument("--orch-id", required=True, help="Remote orch_id")
    rm.add_argument("--workspace-id", required=True, help="Workspace UUID")

    rot = peer_sub.add_parser(
        "rotate",
        help="Rotate an accepted peer's token (D15/NG8 grace)",
    )
    rot.add_argument("--orch-id", required=True, help="Remote orch_id")
    rot.add_argument(
        "--workspace-id", required=True, help="Workspace UUID"
    )
    rot.add_argument(
        "--grace-seconds",
        type=float,
        default=None,
        help=(
            "Old-token grace window override (default: "
            "ORCHESTRATORD_PEER_TOKEN_GRACE_SECONDS, 300s)"
        ),
    )

    leave = peer_sub.add_parser("leave", help="Leave a peer group")
    leave.add_argument("--group-id", required=True)
    leave.add_argument(
        "--member",
        default=None,
        help="Member to remove (default: this daemon's orch_id)",
    )

    group_sub = peer_sub.add_parser("group", help="Manage peer groups (D26)")
    group_children = group_sub.add_subparsers(
        dest="group_subcommand", required=True
    )
    group_children.add_parser("list", help="List local groups")
    create = group_children.add_parser("create", help="Create a group")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--member",
        action="append",
        required=True,
        help="Member orch_id (repeatable)",
    )
    add = group_children.add_parser("add", help="Add a member (caller must be one)")
    add.add_argument("--group-id", required=True)
    add.add_argument("--member", required=True)
    add.add_argument(
        "--caller", default=None, help="Caller orch_id (default: local)"
    )
    kick = group_children.add_parser(
        "remove", help="Remove a member (caller must be one, D26)"
    )
    kick.add_argument("--group-id", required=True)
    kick.add_argument("--member", required=True)
    kick.add_argument(
        "--caller", default=None, help="Caller orch_id (default: local)"
    )


def run(args: argparse.Namespace) -> int:
    """Dispatch the chosen ``peer`` subcommand."""
    if args.peer_subcommand == "list":
        return asyncio.run(_run_list(UUID(args.workspace_id), args.status))
    if args.peer_subcommand == "invite":
        return _run_invite(args.url, UUID(args.workspace_id), args.name)
    if args.peer_subcommand == "accept":
        return asyncio.run(_run_accept(UUID(args.peer_id)))
    if args.peer_subcommand == "reject":
        return asyncio.run(_run_reject(UUID(args.peer_id)))
    if args.peer_subcommand == "remove":
        return asyncio.run(_run_remove(UUID(args.workspace_id), args.orch_id))
    if args.peer_subcommand == "rotate":
        return asyncio.run(
            _run_rotate(
                UUID(args.workspace_id),
                args.orch_id,
                getattr(args, "grace_seconds", None),
            )
        )
    if args.peer_subcommand == "leave":
        return asyncio.run(
            _run_leave(args.group_id, args.member or ensure_orch_id())
        )
    if args.peer_subcommand == "group":
        return _run_group(args)
    print(f"Unknown peer subcommand: {args.peer_subcommand}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Registry verbs (direct DB, same registry functions the API uses)
# ---------------------------------------------------------------------------


async def _run_list(workspace_id: UUID, status: str | None) -> int:
    from orchestratord.db.engine import build_session_factory

    async with build_session_factory()() as session:
        rows = await list_peers(session, workspace_id, status=status)
    if not rows:
        print("(no peers)")
        return 0
    print(
        f"{'ORCH_ID':<24} {'STATUS':<10} {'CLIENT_KIND':<12} NAME / URL"
    )
    for row in rows:
        # PR-B1: a trailing ⚠ marks legacy (Phase 1) peers so the operator
        # can spot them at a glance. The column is always present so
        # ``awk``/``jq`` pipelines downstream keep a stable shape.
        marker = " ⚠" if row.client_kind == CLIENT_KIND_V1_SUNSET else ""
        print(
            f"{row.orch_id:<24} {row.status:<10} "
            f"{row.client_kind:<12} {row.name} / {row.url}{marker}"
        )
    return 0


async def _run_accept(peer_id: UUID) -> int:
    """Operator accept: issue the per-peer token (D15, shown once)."""
    from orchestratord.db.engine import build_session_factory

    async with build_session_factory()() as session:
        peer = await session.get(orm.Peer, peer_id)
        if peer is None:
            print(f"peer not found: {peer_id}", file=sys.stderr)
            return 1
        plaintext, token_hash = issue_api_token()
        token_row = orm.AuthToken(
            id=uuid.uuid4(),
            workspace_id=peer.workspace_id,
            name=f"peer:{peer.orch_id}",
            token_hash=token_hash,
            scopes=["peer.*"],
            expires_at=None,
            created_at=datetime.now(UTC),
        )
        session.add(token_row)
        peer.token_id = token_row.id
        await set_peer_status(session, peer, "accepted")
        await session.commit()
    print(f"accepted {peer.orch_id}")
    print(f"token (shown once): {plaintext}")
    return 0


async def _run_reject(peer_id: UUID) -> int:
    from orchestratord.db.engine import build_session_factory

    async with build_session_factory()() as session:
        peer = await session.get(orm.Peer, peer_id)
        if peer is None:
            print(f"peer not found: {peer_id}", file=sys.stderr)
            return 1
        orch_id = peer.orch_id
        await session.delete(peer)
        await session.commit()
    print(f"rejected {orch_id}")
    return 0


async def _run_remove(workspace_id: UUID, orch_id: str) -> int:
    from orchestratord.db.engine import build_session_factory

    async with build_session_factory()() as session:
        removed = await remove_peer(session, workspace_id, orch_id)
        await session.commit()
    if not removed:
        print(f"peer not found: {orch_id}", file=sys.stderr)
        return 1
    print(f"removed {orch_id}")
    return 0


async def _run_rotate(
    workspace_id: UUID, orch_id: str, grace_seconds: float | None
) -> int:
    """Rotate an accepted peer's token (D15/NG8); plaintext shown once."""
    from orchestratord.config.schema import PeerConfig
    from orchestratord.db.engine import build_session_factory
    from orchestratord.peer.registry import get_peer, rotate_peer_token

    if grace_seconds is None:
        grace_seconds = PeerConfig.from_env().token_grace_seconds
    async with build_session_factory()() as session:
        peer = await get_peer(session, workspace_id, orch_id)
        if peer is None or peer.status != "accepted":
            print(
                f"accepted peer not found: {orch_id}", file=sys.stderr
            )
            return 1
        try:
            plaintext, old_expires_at = await rotate_peer_token(
                session, peer, grace_seconds=grace_seconds
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        await session.commit()
    print(f"rotated token for {orch_id}")
    print(f"grace_seconds: {grace_seconds}")
    print(
        "old token expires at: "
        + (
            old_expires_at.isoformat()
            if old_expires_at is not None
            else "(was unbound)"
        )
    )
    print(f"token (shown once): {plaintext}")
    return 0


def _run_invite(url: str, workspace_id: UUID, name: str | None) -> int:
    """POST this daemon's identity to a remote ``/api/peer/invite``."""
    payload = {
        "orch_id": ensure_orch_id(),
        "name": name or ensure_orch_id(),
        "url": os.environ.get("ORCHESTRATORD_PEER_PUBLIC_URL", ""),
        "workspace_id": str(workspace_id),
        "capabilities": ["peer.invoke"],
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/api/peer/invite",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"invite rejected by {url}: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"cannot reach {url}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0 if body.get("status") in ("pending", "accepted") else 1


# ---------------------------------------------------------------------------
# Group verbs (D26: membership-only authorization, no owner)
# ---------------------------------------------------------------------------

# Process-global group manager: group state is in-memory with Redis
# convergence, so sequential CLI invocations inside one process (and the
# test suite) must see the same manager, mirroring the dispatcher singleton.
_group_manager: GroupManager | None = None


def get_group_manager() -> GroupManager:
    global _group_manager
    if _group_manager is None:
        _group_manager = GroupManager(local_orch_id=ensure_orch_id())
    return _group_manager


def reset_group_manager() -> None:
    """Drop the process-wide manager (test seam)."""
    global _group_manager
    _group_manager = None


def _run_group(args: argparse.Namespace) -> int:
    local = ensure_orch_id()
    manager = get_group_manager()
    try:
        if args.group_subcommand == "list":
            groups = manager.all_groups()
            if not groups:
                print("(no groups)")
                return 0
            for grp in groups:
                print(f"{grp.group_id}  {grp.name}  members={sorted(grp.members)}")
            return 0
        if args.group_subcommand == "create":
            grp = asyncio.run(
                manager.create_group(name=args.name, members=set(args.member))
            )
            print(f"created {grp.group_id}  members={sorted(grp.members)}")
            return 0
        if args.group_subcommand == "add":
            grp = asyncio.run(
                manager.add_member(args.group_id, args.member, caller=args.caller or local)
            )
            print(f"{grp.group_id}  members={sorted(grp.members)}")
            return 0
        if args.group_subcommand == "remove":
            grp = asyncio.run(
                manager.remove_member(args.group_id, args.member, caller=args.caller or local)
            )
            print(f"{grp.group_id}  members={sorted(grp.members)}")
            return 0
    except GroupError as exc:
        print(f"group error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown group subcommand: {args.group_subcommand}", file=sys.stderr)
    return 2


async def _run_leave(group_id: str, member: str) -> int:
    manager = get_group_manager()
    try:
        grp = await manager.leave(group_id, member)
    except GroupError as exc:
        print(f"group error: {exc}", file=sys.stderr)
        return 1
    print(f"{grp.group_id}  members={sorted(grp.members)}")
    return 0
