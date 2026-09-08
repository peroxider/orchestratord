"""Collaboration models (§7.1–§7.3): squads, projects, autopilots."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Squad(Base):
    __tablename__ = "squads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    name: Mapped[str]
    leader_type: Mapped[str]
    leader_id: Mapped[uuid.UUID]
    is_deleted: Mapped[bool]
    created_at: Mapped[datetime]


class SquadMember(Base):
    __tablename__ = "squad_members"

    squad_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    member_type: Mapped[str] = mapped_column(primary_key=True)
    member_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    name: Mapped[str]
    description: Mapped[str]


class ProjectRepo(Base):
    __tablename__ = "project_repos"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID]
    repo_url: Mapped[str]
    default_branch: Mapped[str]


class ProjectDoc(Base):
    __tablename__ = "project_docs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    project_id: Mapped[uuid.UUID]
    doc_url: Mapped[str]
    doc_type: Mapped[str]


class Autopilot(Base):
    __tablename__ = "autopilots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    name: Mapped[str]
    cron: Mapped[str]
    prompt: Mapped[str]
    target_kind: Mapped[str]
    target_id: Mapped[uuid.UUID]
    enabled: Mapped[bool]


class AutopilotRun(Base):
    __tablename__ = "autopilot_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    autopilot_id: Mapped[uuid.UUID]
    scheduled_at: Mapped[datetime]
    run_id: Mapped[uuid.UUID]
    status: Mapped[str]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
