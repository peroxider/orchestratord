"""Tests for the ``orchestratord db`` subcommand (§3.5).

All three verbs are exercised against fakes: no database is touched.
``init``/``reset`` fake the SQLAlchemy engine, ``migrate`` patches
``alembic.command.upgrade`` and asserts the DSN coercion
(``+asyncpg`` → ``+psycopg2``) applied to the Alembic config.
"""

from __future__ import annotations

import argparse

import pytest

from orchestratord.cli.db import _alembic_config, add_db_parser, run

TEST_URL = (
    "postgresql+asyncpg://multica:multica@127.0.0.1:5432/orchestratord_test"
)
SYNC_URL = TEST_URL.replace("+asyncpg", "+psycopg2")


def _db_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top", required=True)
    add_db_parser(subparsers)
    return parser.parse_args(["db", *argv])


@pytest.fixture
def isolated_env(monkeypatch):
    monkeypatch.setenv("ORCHESTRATORD_DATABASE_URL", TEST_URL)


@pytest.fixture
def tmp_ini(tmp_path, monkeypatch):
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = alembic\n", encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATORD_ALEMBIC_INI", str(ini))
    return ini


class TestParser:
    def test_verbs_required(self) -> None:
        with pytest.raises(SystemExit):
            _db_args([])

    def test_reset_confirm_flag(self) -> None:
        assert _db_args(["reset", "--yes"]).yes is True
        assert _db_args(["reset"]).yes is False


class TestInit:
    def test_creates_schema(self, isolated_env, monkeypatch, capsys) -> None:
        calls: list = []

        async def fake_create_schema(engine):
            calls.append(("create", engine))

        monkeypatch.setattr(
            "orchestratord.db.engine.create_schema", fake_create_schema
        )
        monkeypatch.setattr(
            "orchestratord.db.engine.build_engine", lambda url=None: "ENGINE"
        )
        assert run(_db_args(["init"])) == 0
        assert calls == [("create", "ENGINE")]
        assert "orchestratord_test" in capsys.readouterr().out


class TestReset:
    def test_refuses_without_yes(self, isolated_env, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            "orchestratord.db.engine.build_engine",
            lambda url=None: (_ for _ in ()).throw(AssertionError("must not build")),
        )
        assert run(_db_args(["reset"])) == 2
        assert "--yes" in capsys.readouterr().err

    def test_yes_drops_and_creates(self, isolated_env, monkeypatch) -> None:
        calls: list = []

        class _FakeConn:
            async def run_sync(self, fn):
                calls.append("run_sync")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _FakeEngine:
            def begin(self):
                return _FakeConn()

            async def dispose(self):
                calls.append("dispose")

        monkeypatch.setattr(
            "orchestratord.db.engine.build_engine", lambda url=None: _FakeEngine()
        )
        assert run(_db_args(["reset", "--yes"])) == 0
        assert calls == ["run_sync", "run_sync", "dispose"]


class TestMigrate:
    def test_config_url_coerced_to_sync_driver(
        self, isolated_env, tmp_ini
    ) -> None:
        cfg = _alembic_config()
        assert cfg is not None
        assert cfg.get_main_option("sqlalchemy.url") == SYNC_URL

    def test_upgrade_runs_to_head(
        self, isolated_env, tmp_ini, monkeypatch
    ) -> None:
        calls: list = []
        monkeypatch.setattr(
            "alembic.command.upgrade", lambda cfg, rev: calls.append(rev)
        )
        assert run(_db_args(["migrate"])) == 0
        assert calls == ["head"]

    def test_missing_driver_message(
        self, isolated_env, tmp_ini, monkeypatch, capsys
    ) -> None:
        def boom(cfg, rev):
            raise ModuleNotFoundError("psycopg2")

        monkeypatch.setattr("alembic.command.upgrade", boom)
        assert run(_db_args(["migrate"])) == 1
        assert "psycopg2" in capsys.readouterr().err

    def test_repo_checkout_fallback(
        self, isolated_env, monkeypatch
    ) -> None:
        monkeypatch.delenv("ORCHESTRATORD_ALEMBIC_INI", raising=False)
        assert _alembic_config() is not None, (
            "the repo checkout must provide alembic.ini for the fallback"
        )
