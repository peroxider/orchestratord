"""Peer-federation registry model (ADR-001 D16: workspace-scoped).

One row per (workspace, remote orchestrator) relationship. Like every
§6.1 table there are no ForeignKeys — referential integrity is
app-layer (see migration 0043's rationale). The one exception to "no
indexes" is ``uq_peers_workspace_orch``: the registry's documented
key must be DB-enforced, or two concurrent invite POSTs can both
read-absent and insert, leaving every later ``get_peer`` for that key
raising ``MultipleResultsFound``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Peer(Base):
    __tablename__ = "peers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "orch_id", name="uq_peers_workspace_orch"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    orch_id: Mapped[str]
    name: Mapped[str]
    url: Mapped[str]
    # D14: same-workspace peers omit this; cross-workspace peers carry
    # the remote workspace's identity explicitly.
    remote_workspace_id: Mapped[str | None]
    # "pending" → "accepted"; removal deletes the row (AC8).
    status: Mapped[str]
    # AuthToken row holding the per-peer bearer token
    # (SHA-256 hash, scopes=["peer.*"]); issued on accept.
    token_id: Mapped[uuid.UUID | None]
    capabilities: Mapped[list[str]] = mapped_column(JSONB)
    card: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
