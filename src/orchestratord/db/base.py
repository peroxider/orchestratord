"""Declarative base + type mapping for the §6.1 PostgreSQL schema.

These models are pure schema definitions: they mirror the
``docs/FEATURE_GAP_VS_MULTICA.md`` §6.1.1 tables (and the DB-agnostic
``orchestratord.domain`` entities) without any driver-specific session/engine
wiring. Per §6.1, no model declares a ``ForeignKey`` and no index is created
inline — indexes are emitted as separate ``CREATE INDEX CONCURRENTLY``
migrations (§6.1 migration rules).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, MetaData, Text, Uuid
from sqlalchemy.orm import DeclarativeBase

_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every §6.1.1 model.

    ``type_annotation_map`` lets models annotate plain Python types
    (``uuid.UUID``, ``datetime``, ``str``, …) and get the PostgreSQL type
    inferred — ``str`` maps to ``TEXT`` (not ``VARCHAR``) and ``datetime`` to
    ``TIMESTAMP WITH TIME ZONE``, matching the tz-aware domain entities.
    """

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy reads this class-level map)
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
        date: Date(),
        int: Integer(),
        float: Float(),
        bool: Boolean(),
        str: Text(),
    }
