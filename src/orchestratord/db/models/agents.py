"""Agent / runtime models (§6.2, §6.3, §6.1.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    name: Mapped[str]
    provider: Mapped[str]
    runtime_id: Mapped[uuid.UUID]
    capabilities_cache_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]


class AgentCapabilitiesCache(Base):
    __tablename__ = "agent_capabilities_cache"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    agent_id: Mapped[uuid.UUID]
    backend_name: Mapped[str]
    capabilities_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[str | None]
    model_pricing_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    refreshed_at: Mapped[datetime]


class Runtime(Base):
    __tablename__ = "runtimes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    hostname: Mapped[str]
    os: Mapped[str]
    token_hash: Mapped[str]
    status: Mapped[str]
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class RuntimeBackend(Base):
    __tablename__ = "runtime_backends"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    runtime_id: Mapped[uuid.UUID]
    backend_name: Mapped[str]
    version: Mapped[str | None]
    probed_at: Mapped[datetime]
