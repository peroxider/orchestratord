"""Inbox + usage models (§5.2.6, §5.2.4, §6.1.1)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class InboxItem(Base):
    __tablename__ = "inbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    kind: Mapped[str]
    title: Mapped[str]
    issue_id: Mapped[uuid.UUID | None]
    session_id: Mapped[uuid.UUID | None]
    event_seq: Mapped[int | None]
    status: Mapped[str]
    assignee_type: Mapped[str | None]
    assignee_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]


class UsageAggregate(Base):
    __tablename__ = "usage_aggregates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    agent_id: Mapped[uuid.UUID | None]
    issue_id: Mapped[uuid.UUID | None]
    backend: Mapped[str]
    day: Mapped[date]
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    cost_usd: Mapped[float]
    sessions: Mapped[int]
