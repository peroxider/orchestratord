"""Session / run / event / approval models (§5.2.3, §6.1.1, §6.1.2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    issue_id: Mapped[uuid.UUID | None]
    agent_id: Mapped[uuid.UUID | None]
    run_id: Mapped[uuid.UUID | None]
    mode: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[datetime]


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    kind: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column()
    session_id: Mapped[uuid.UUID]
    sequence: Mapped[int]
    kind: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    run_id: Mapped[uuid.UUID | None]
    issue_id: Mapped[uuid.UUID | None]
    workspace_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID]
    request_id: Mapped[str]
    decision: Mapped[str | None]
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]


class Message(Base):
    """Chat message (one conversational turn) for a session (§6.1).

    The high-level shape the chat UI renders: each row collapses a span of
    SPI events — one ``user`` POST, or many ``text_delta`` events folded
    into a single ``assistant`` turn — into a renderable unit. ``role``
    ∈ {user, assistant, system, tool}. ``seq`` is per-session monotonically
    increasing, assigned by the repository at insert time so concurrent
    appends from the runner don't interleave with user posts.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Backend integrity for the per-session seq: the repository locks
        # the parent session row when assigning MAX+1, and this constraint
        # catches any residual path that would corrupt the after_seq
        # tail-fetch contract (§6.1a).
        UniqueConstraint("session_id", "seq", name="uq_messages_session_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID]
    workspace_id: Mapped[uuid.UUID]
    seq: Mapped[int]
    role: Mapped[str]
    content: Mapped[str]
    agent_id: Mapped[uuid.UUID | None]
    author_label: Mapped[str | None]
    created_at: Mapped[datetime]
