"""Workspace-scoped peer registry (ADR-001 D16; D14 boundary fields).

The ``peers`` table lives beside the other workspace tables, so
per-workspace backup/restore automatically includes peer state (AC22).
Same-workspace peers omit ``remote_workspace_id``; cross-workspace
peers carry it explicitly (D14).

Functions take an :class:`AsyncSession` — the peer layer deliberately
does not extend the shared ``Repositories`` aggregate, keeping all
peer data access inside the peer package (D5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestratord.db.models.peer import Peer

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"

# PR-B1: stable client-version tag stamped on each peer row. ``v1_sunset``
# marks Phase 1 clients (no transports[] in card, no peer_client_version
# in invite); ``v2`` marks Phase B clients that opt in. Migration 0048
# backfills existing rows with ``v1_sunset``.
CLIENT_KIND_V1_SUNSET = "v1_sunset"
CLIENT_KIND_V2 = "v2"


def _now() -> datetime:
    return datetime.now(UTC)


async def get_peer(
    session: AsyncSession, workspace_id: uuid.UUID, orch_id: str
) -> Peer | None:
    result = await session.execute(
        select(Peer).where(
            Peer.workspace_id == workspace_id, Peer.orch_id == orch_id
        )
    )
    return result.scalar_one_or_none()


async def get_peer_by_token_id(
    session: AsyncSession, token_id: uuid.UUID, orch_id: str
) -> Peer | None:
    """Resolve the registry row an inbound peer request authenticated as.

    Binds the bearer token (an ``auth_tokens`` row with a ``peer.*``
    scope) to the (orch_id, token) pair so a peer cannot impersonate
    another peer's identity header.
    """
    result = await session.execute(
        select(Peer).where(Peer.token_id == token_id, Peer.orch_id == orch_id)
    )
    return result.scalar_one_or_none()


async def list_peers(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: str | None = None,
) -> list[Peer]:
    stmt = select(Peer).where(Peer.workspace_id == workspace_id)
    if status is not None:
        stmt = stmt.where(Peer.status == status)
    result = await session.execute(stmt.order_by(Peer.created_at))
    return list(result.scalars().all())


async def upsert_peer(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    orch_id: str,
    name: str,
    url: str,
    capabilities: list[str] | None = None,
    remote_workspace_id: str | None = None,
    card: dict[str, Any] | None = None,
    client_kind: str | None = None,
) -> Peer:
    """Create or refresh the registry row for (workspace_id, orch_id).

    New rows start ``pending`` — trust is granted only via
    :func:`set_peer_status` after the accept handshake. Discovery
    fields (name/url/capabilities/card) refresh in place without
    touching trust state, so a re-invite never silently resurrects a
    peer the operator rejected.

    PR-B1: ``client_kind`` is stamped on INSERT (defaulting to
    :data:`CLIENT_KIND_V1_SUNSET` when the caller does not specify it)
    and is **preserved on UPDATE** — a re-invite that lacks
    ``peer_client_version`` must not downgrade an already-classified
    peer back to ``v1_sunset``. Callers that need to force-update the
    classification can pass the new value explicitly.

    The read-then-insert is race-guarded by ``uq_peers_workspace_orch``:
    a concurrent invite for the same key loses its INSERT inside a
    SAVEPOINT, and the loop re-reads the winning row — the caller always
    gets the one registry row, never a duplicate.
    """
    for _ in range(2):
        peer = await get_peer(session, workspace_id, orch_id)
        if peer is not None:
            peer.name = name
            peer.url = url
            peer.remote_workspace_id = remote_workspace_id
            peer.capabilities = list(capabilities or [])
            peer.card = card
            # PR-B1: client_kind is intentionally NOT refreshed here —
            # see docstring invariant. Caller passes explicit value to
            # override.
            await session.flush()
            return peer
        peer = Peer(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            orch_id=orch_id,
            name=name,
            url=url,
            remote_workspace_id=remote_workspace_id,
            status=STATUS_PENDING,
            token_id=None,
            capabilities=list(capabilities or []),
            card=card,
            client_kind=client_kind
            if client_kind is not None
            else CLIENT_KIND_V1_SUNSET,
            created_at=_now(),
            accepted_at=None,
        )
        try:
            async with session.begin_nested():
                # The add must live inside the savepoint: a losing INSERT
                # then rolls back with it, leaving the session clean for
                # the re-read below. Added outside, the failed INSERT
                # stays pending in the outer transaction and the session
                # wedges into PendingRollbackError instead of converging.
                session.add(peer)
                await session.flush()
        except IntegrityError:
            # Another transaction inserted the same key first; the
            # savepoint discarded our INSERT, so re-read the winner.
            peer = None
            continue
        return peer
    raise RuntimeError(
        f"upsert_peer lost the uq_peers_workspace_orch race twice for "
        f"(workspace_id={workspace_id}, orch_id={orch_id!r})"
    )


async def set_peer_status(session: AsyncSession, peer: Peer, status: str) -> Peer:
    """Transition trust state; ``accepted`` stamps ``accepted_at``."""
    peer.status = status
    if status == STATUS_ACCEPTED:
        peer.accepted_at = _now()
    await session.flush()
    return peer


async def remove_peer(
    session: AsyncSession, workspace_id: uuid.UUID, orch_id: str
) -> bool:
    """Delete the registry row (AC8: removed peers lose access)."""
    result = await session.execute(
        delete(Peer).where(
            Peer.workspace_id == workspace_id, Peer.orch_id == orch_id
        )
    )
    return result.rowcount > 0
