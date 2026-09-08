"""DDL-compilation contract tests (§6.1 schema, no live DB).

Each model's ``CREATE TABLE`` is rendered against the PostgreSQL dialect and
the result asserted: ``JSONB`` stays ``JSONB`` (not ``VARCHAR``), tz-aware
``datetime`` renders ``TIMESTAMP WITH TIME ZONE``, and ``events`` renders
``PARTITION BY RANGE``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import orchestratord.db  # noqa: F401  (registers all tables on Base.metadata)
from orchestratord.db.base import Base

DIALECT = postgresql.dialect()


def _ddl(table_name: str) -> str:
    return str(
        CreateTable(Base.metadata.tables[table_name]).compile(dialect=DIALECT)
    )


def test_every_table_compiles() -> None:
    for table_name in Base.metadata.tables:
        ddl = _ddl(table_name)
        assert "CREATE TABLE" in ddl, f"{table_name} did not render CREATE TABLE"


def test_events_partition_by_range_renders() -> None:
    assert "PARTITION BY RANGE" in _ddl("events")


def test_events_created_at_renders_timestamptz() -> None:
    column = Base.metadata.tables["events"].c.created_at
    assert column.type.compile(dialect=DIALECT) == "TIMESTAMP WITH TIME ZONE"


def test_events_payload_renders_jsonb() -> None:
    column = Base.metadata.tables["events"].c.payload
    assert column.type.compile(dialect=DIALECT) == "JSONB"


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("agents", "capabilities_cache_jsonb"),
        ("agent_capabilities_cache", "capabilities_jsonb"),
        ("agent_capabilities_cache", "model_pricing_jsonb"),
        ("skills", "allowed_tools"),
        ("skills", "stale_reasons"),
        ("audit_log", "payload_jsonb"),
        ("auth_tokens", "scopes"),
    ],
)
def test_jsonb_columns_render_jsonb(table_name: str, column_name: str) -> None:
    column = Base.metadata.tables[table_name].c[column_name]
    assert column.type.compile(dialect=DIALECT) == "JSONB"
