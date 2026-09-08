"""Tenancy models (§5.7.1, §6.1): workspaces, members, member_agent_scopes."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    slug: Mapped[str]
    name: Mapped[str]
    created_at: Mapped[datetime]


class Member(Base):
    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    role: Mapped[str]
    name: Mapped[str]
    created_at: Mapped[datetime]


class MemberAgentScope(Base):
    __tablename__ = "member_agent_scopes"

    member_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
