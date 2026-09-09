"""Alembic migration environment (§6.1).

Offline-capable: ``alembic upgrade head --sql`` renders the full DDL without a
database connection (the "No live DB" verification path). The version table is
application-specific so a shared database's migration ledger is not overwritten.
Each ``CREATE INDEX CONCURRENTLY``
migration runs outside a transaction via ``autocommit_block()`` (§6.1: "runner
在迁移文件外执行以支持非事务场景").
"""

from __future__ import annotations

import asyncio

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from orchestratord.db import models  # noqa: F401  (registers tables on metadata)
from orchestratord.db.base import Base
from orchestratord.db.engine import database_url

config = context.config
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
version_table = config.get_main_option("version_table")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without a DB connection (``--sql``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=version_table,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Connect with the same asyncpg engine used by the running service."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
