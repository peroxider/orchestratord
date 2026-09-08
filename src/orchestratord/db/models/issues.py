"""Issue models (§5.2.1, §6.1.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    title: Mapped[str]
    description: Mapped[str]
    status: Mapped[str]
    assignee_type: Mapped[str | None]
    assignee_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]


class IssueComment(Base):
    __tablename__ = "issue_comments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    issue_id: Mapped[uuid.UUID]
    author_type: Mapped[str]
    author_id: Mapped[uuid.UUID]
    body: Mapped[str]
    created_at: Mapped[datetime]


class IssueLabel(Base):
    __tablename__ = "issue_labels"

    issue_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)


class IssueStatusChange(Base):
    __tablename__ = "issue_status_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    issue_id: Mapped[uuid.UUID]
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    created_at: Mapped[datetime]
