"""``orchestratord db`` — schema lifecycle (install.sh §3.5).

Three verbs:

* ``init`` — ``Base.metadata.create_all`` via the asyncpg engine (local
  bring-up / tests; no indexes).
* ``migrate`` — apply Alembic migrations to head. The repo ``alembic.ini``
  is located from the checkout (or ``ORCHESTRATORD_ALEMBIC_INI``) and its
  ``sqlalchemy.url`` is overridden with the daemon DSN
  (``ORCHESTRATORD_DATABASE_URL``) coerced to the sync psycopg2 driver
  that Alembic's env.py expects.
* ``reset`` — drop and recreate every table; destructive, requires ``--yes``.

Per §3.5 this must not break existing CLI compatibility: it is a pure
addition.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def add_db_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``db`` subcommand and its verbs."""
    db_parser = subparsers.add_parser(
        "db",
        help="Database schema lifecycle (init / migrate / reset)",
        description="Schema bring-up and migration helpers (§3.5).",
    )
    db_sub = db_parser.add_subparsers(dest="db_subcommand", required=True)
    db_sub.add_parser(
        "init",
        help="Create all tables via create_all (no indexes; local bring-up)",
    )
    db_sub.add_parser(
        "migrate",
        help="Apply Alembic migrations to head",
    )
    reset = db_sub.add_parser(
        "reset",
        help="Drop and recreate every table (DESTRUCTIVE)",
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the destructive drop + recreate",
    )


def run(args: argparse.Namespace) -> int:
    """Dispatch the chosen ``db`` verb."""
    if args.db_subcommand == "init":
        return _init()
    if args.db_subcommand == "migrate":
        return _migrate()
    if args.db_subcommand == "reset":
        return _reset(yes=args.yes)
    print(f"Unknown db subcommand: {args.db_subcommand}", file=sys.stderr)
    return 2


def _init() -> int:
    from orchestratord.db.engine import build_engine, create_schema

    asyncio.run(create_schema(build_engine()))
    print(
        "schema created on "
        f"{os.environ.get('ORCHESTRATORD_DATABASE_URL') or '(default DSN)'}"
    )
    return 0


def _alembic_config():
    """Alembic Config from the checkout, pointed at the daemon DSN.

    Returns ``None`` when no ``alembic.ini`` is available (installed
    wheel without a repo checkout).
    """
    from alembic.config import Config

    ini = os.environ.get("ORCHESTRATORD_ALEMBIC_INI")
    if ini:
        path = Path(ini)
    else:
        # src/orchestratord/cli/db.py → parents[3] is the repo root.
        path = Path(__file__).resolve().parents[3] / "alembic.ini"
    if not path.is_file():
        return None
    cfg = Config(str(path))
    url = _sync_database_url()
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _sync_database_url() -> str | None:
    """The daemon DSN with the async driver swapped for a sync one.

    Alembic's env.py builds a sync engine; the daemon's
    ``postgresql+asyncpg://`` URL would be rejected there.
    """
    from orchestratord.db.engine import database_url

    return database_url().replace("+asyncpg", "+psycopg2")


def _migrate() -> int:
    from alembic import command

    cfg = _alembic_config()
    if cfg is None:
        print(
            "orchestratord db migrate: alembic.ini not found — point "
            "ORCHESTRATORD_ALEMBIC_INI at a checkout's alembic.ini",
            file=sys.stderr,
        )
        return 1
    try:
        command.upgrade(cfg, "head")
    except ModuleNotFoundError as exc:
        print(
            f"orchestratord db migrate: missing driver {exc.name!r} — "
            "install psycopg2-binary (Alembic env.py uses a sync engine)",
            file=sys.stderr,
        )
        return 1
    return 0


def _reset(yes: bool) -> int:
    if not yes:
        print(
            "orchestratord db reset: refusing to drop all tables without "
            "--yes (destructive)",
            file=sys.stderr,
        )
        return 2
    from orchestratord.db.base import Base
    from orchestratord.db.engine import build_engine

    async def _drop_and_create() -> None:
        engine = build_engine()
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(_drop_and_create())
    print("schema reset complete")
    return 0
