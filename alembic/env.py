"""Alembic migration environment (§6.1).

Offline-capable: ``alembic upgrade head --sql`` renders the full DDL without a
database connection (the "No live DB" verification path). The version table is
``schema_migrations`` (§6.1 migration rules). Each ``CREATE INDEX CONCURRENTLY``
migration runs outside a transaction via ``autocommit_block()`` (§6.1: "runner
在迁移文件外执行以支持非事务场景").
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from orchestratord.db import models  # noqa: F401  (registers tables on metadata)
from orchestratord.db.base import Base

config = context.config

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without a DB connection (``--sql``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="schema_migrations",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run against a live database (not exercised this increment)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="schema_migrations",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
