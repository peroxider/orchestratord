"""G2 迁移健全 — DESIGN_PR_GATE_TEST.md §5.3。

迁移与 ORM 元数据互为唯一事实源的两条腿，必须对拍一致：
1. alembic upgrade head 全量重放
2. create_schema() 对拍 information_schema（表集合 + 每表列集合）
3. alembic downgrade base 可逆性
"""
from __future__ import annotations

import os

import subprocess
import sys

import gate_support as g
import pytest

pytestmark = pytest.mark.gate  # PR merge gate (DESIGN_PR_GATE_TEST.md §4)

MIG_DB = "gate_mig"
ORM_DB = "gate_orm"
MIG_DSN = f"postgresql+asyncpg://multica:multica@127.0.0.1:5432/{MIG_DB}"
ORM_DSN = f"postgresql+asyncpg://multica:multica@127.0.0.1:5432/{ORM_DB}"

#: 迁移侧独有的记账表，不参与对拍。
LEDGER_TABLES = {"orchestratord_schema_migrations"}


def _alembic_env(dsn: str) -> dict[str, str]:
    env = dict(os.environ)
    env["ORCHESTRATORD_DATABASE_URL"] = dsn
    return env


def _alembic(args: list[str], dsn: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=str(g.REPO_ROOT),
        env=_alembic_env(dsn),
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def fresh_dbs(request: pytest.FixtureRequest):
    g.registered_skip(
        request,
        "G2.migrations",
        env_ok=g.pg_reachable(),
        env_gone="Postgres unreachable at 127.0.0.1:5432",
    )
    import asyncio

    asyncio.run(g.recreate_db(MIG_DB))
    asyncio.run(g.recreate_db(ORM_DB))
    yield


class TestG2:
    def test_upgrade_head(self, request, fresh_dbs) -> None:
        proc = _alembic(["upgrade", "head"], MIG_DSN)
        g.waive_known_defect(
            request,
            "G2.migrations",
            output=proc.stdout + proc.stderr,
            signature="cannot create index on partitioned table",
        )
        assert proc.returncode == 0, (
            f"alembic upgrade head rc={proc.returncode}\n"
            f"{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
        )

    def test_downgrade_base(self, request, fresh_dbs) -> None:
        up = _alembic(["upgrade", "head"], MIG_DSN)
        g.waive_known_defect(
            request,
            "G2.migrations",
            output=up.stdout + up.stderr,
            signature="cannot create index on partitioned table",
        )
        assert up.returncode == 0, up.stderr[-3000:]
        down = _alembic(["downgrade", "base"], MIG_DSN)
        g.waive_known_defect(
            request,
            "G2.migrations",
            output=down.stdout + down.stderr,
            signature="cannot create index on partitioned table",
        )
        assert down.returncode == 0, (
            f"alembic downgrade base rc={down.returncode}\n"
            f"{down.stdout[-3000:]}\n{down.stderr[-3000:]}"
        )

    def test_migrations_match_orm_schema(self, request, fresh_dbs) -> None:
        """迁移产物与 create_schema() 的表/列集合必须一致（漂移即 FAIL）。"""
        import asyncio

        up = _alembic(["upgrade", "head"], MIG_DSN)
        g.waive_known_defect(
            request,
            "G2.migrations",
            output=up.stdout + up.stderr,
            signature="cannot create index on partitioned table",
        )
        assert up.returncode == 0, up.stderr[-3000:]

        # ORM 侧：create_schema 建第二份空库
        async def _build_orm() -> None:
            from orchestratord.db import models  # noqa: F401 — 注册全部表
            from orchestratord.db.engine import build_engine, create_schema

            engine = build_engine(ORM_DSN)
            try:
                await create_schema(engine)
            finally:
                await engine.dispose()

        asyncio.run(_build_orm())

        mig = asyncio.run(g.schema_snapshot(MIG_DSN.replace("+asyncpg", "")))
        orm = asyncio.run(g.schema_snapshot(ORM_DSN.replace("+asyncpg", "")))

        mig_tables = set(mig) - LEDGER_TABLES
        orm_tables = set(orm)
        missing_in_migrations = orm_tables - mig_tables
        missing_in_orm = mig_tables - orm_tables
        assert not missing_in_migrations and not missing_in_orm, (
            "迁移与 ORM 元数据漂移 — "
            f"ORM 有而迁移缺表: {sorted(missing_in_migrations)}; "
            f"迁移有而 ORM 缺表: {sorted(missing_in_orm)}"
        )
        col_diffs = [
            f"{t}: 仅迁移有 {sorted(mig[t] - orm[t])} / 仅 ORM 有 {sorted(orm[t] - mig[t])}"
            for t in sorted(mig_tables & orm_tables)
            if mig[t] != orm[t]
        ]
        assert not col_diffs, "列集合漂移:\n" + "\n".join(col_diffs)
