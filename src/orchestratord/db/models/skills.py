"""Skill models (§6.1.1): skills, skill_source_maps, skill_references."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    display_name: Mapped[str]
    description: Mapped[str]
    user_invocable: Mapped[bool]
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB)
    version: Mapped[int]
    skill_md_path: Mapped[str]
    is_stale: Mapped[bool]
    stale_reasons: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime]


class SkillSourceMap(Base):
    __tablename__ = "skill_source_maps"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    skill_id: Mapped[uuid.UUID]
    path: Mapped[str]
    content_hash: Mapped[str]


class SkillReference(Base):
    __tablename__ = "skill_references"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    skill_id: Mapped[uuid.UUID]
    source_map_id: Mapped[uuid.UUID]
    file_path: Mapped[str]
    start_line: Mapped[int]
    end_line: Mapped[int]
    sha_prefix: Mapped[str]
    claim: Mapped[str]
