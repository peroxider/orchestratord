"""Tests for the §10.2 first-boot seed and its serve wiring.

DB-level tests run against the dedicated ``orchestratord_test`` database
(same DSN as ``tests/api/conftest.py``) and clean up exactly the three
seed rows they create — the seed's fixed member UUID makes that
surgical. Serve-wiring tests are DB-free: ``run_seed`` is monkeypatched
and ``os.environ`` swapped for a copy (serve.run's setdefault leak).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import types

import pytest
from sqlalchemy import delete, func, select

from orchestratord.seed import (
    DAEMON_TOKEN_NAME,
    DEFAULT_OWNER_MEMBER_ID,
    DEFAULT_WORKSPACE_SLUG,
    hash_token,
    seed_default_workspace,
)

TEST_DSN = "postgresql+asyncpg://multica:multica@127.0.0.1:5432/orchestratord_test"


def _make_serve_args(argv: list[str]) -> argparse.Namespace:
    from orchestratord.cli.serve import add_serve_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top", required=True)
    add_serve_parser(subparsers)
    return parser.parse_args(["serve", *argv])


@pytest.fixture
def isolated_env(monkeypatch):
    """serve.run mutates os.environ via setdefault — keep it hermetic."""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("ORCHESTRATORD_DATABASE_URL", TEST_DSN)


class TestHashToken:
    def test_known_vector(self) -> None:
        assert (
            hash_token("abc")
            == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_differs_from_plaintext(self) -> None:
        assert hash_token("secret") != "secret"


class TestSeedDefaultWorkspace:
    @staticmethod
    async def _cleanup(engine) -> None:
        from orchestratord.db.models.audit_auth import AuthToken
        from orchestratord.db.models.tenancy import Member, Workspace

        async with engine.begin() as conn:
            await conn.execute(
                delete(AuthToken).where(AuthToken.name == DAEMON_TOKEN_NAME)
            )
            await conn.execute(
                delete(Member).where(Member.id == DEFAULT_OWNER_MEMBER_ID)
            )
            await conn.execute(
                delete(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
            )

    @pytest.fixture
    async def seed_factory(self, monkeypatch):
        from orchestratord.db.engine import build_engine, build_session_factory

        try:
            engine = build_engine(TEST_DSN)
            async with engine.connect():
                pass
        except Exception as exc:  # noqa: BLE001 — env guard: skip, not crash
            pytest.skip(f"Postgres unavailable for seed tests: {exc}")
        await self._cleanup(engine)
        yield build_session_factory(engine)
        await self._cleanup(engine)
        await engine.dispose()

    async def test_seeds_workspace_member_token(self, seed_factory) -> None:
        from orchestratord.db.models.audit_auth import AuthToken
        from orchestratord.db.models.tenancy import Member, Workspace

        result = await seed_default_workspace(seed_factory)
        assert result is not None

        async with seed_factory() as db:
            ws = await db.scalar(
                select(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
            )
            assert ws is not None
            assert ws.id == result.workspace_id
            member = await db.get(Member, DEFAULT_OWNER_MEMBER_ID)
            assert member is not None
            assert member.role == "owner"
            assert member.workspace_id == ws.id
            token = await db.scalar(
                select(AuthToken).where(AuthToken.workspace_id == ws.id)
            )
            assert token is not None
            assert token.name == DAEMON_TOKEN_NAME
            assert token.token_hash == hash_token(result.token_plaintext)
            assert token.scopes == ["runtime"]

    async def test_second_run_is_noop(self, seed_factory) -> None:
        from orchestratord.db.models.tenancy import Workspace

        first = await seed_default_workspace(seed_factory)
        assert first is not None
        assert await seed_default_workspace(seed_factory) is None

        async with seed_factory() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(Workspace)
                .where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
            )
        assert count == 1

    async def test_plaintext_never_persisted(self, seed_factory) -> None:
        from orchestratord.db.models.audit_auth import AuthToken

        result = await seed_default_workspace(seed_factory)
        assert result is not None
        async with seed_factory() as db:
            rows = (await db.scalars(select(AuthToken))).all()
        assert rows, "expected the daemon-runtime token row to exist"
        assert result.token_plaintext not in {r.token_hash for r in rows}
        assert all(
            len(r.token_hash) == 64
            and all(c in "0123456789abcdef" for c in r.token_hash)
            for r in rows
        ), "only sha256 hex digests may be stored"


class TestServeWiring:
    @staticmethod
    def _patch_uvicorn(monkeypatch) -> None:
        # serve.run() builds a uvicorn.Config and drives a Server subclass
        # (the D24 GOODBYE-drain override), so the fake needs both.
        class FakeServer:
            def __init__(self, config) -> None:
                self.config = config

            def run(self) -> None:
                pass

        monkeypatch.setitem(
            sys.modules,
            "uvicorn",
            types.SimpleNamespace(
                Config=lambda app, **kw: types.SimpleNamespace(app=app, **kw),
                Server=FakeServer,
            ),
        )

    def test_seeds_by_default(self, isolated_env, monkeypatch) -> None:
        calls: list = []
        monkeypatch.setattr(
            "orchestratord.seed.run_seed", lambda: calls.append(1) or None
        )
        self._patch_uvicorn(monkeypatch)
        from orchestratord.cli.serve import run as serve_run

        assert serve_run(_make_serve_args([])) == 0
        assert len(calls) == 1

    def test_no_seed_flag_skips(self, isolated_env, monkeypatch) -> None:
        def boom():
            raise AssertionError("run_seed must not be called with --no-seed")

        monkeypatch.setattr("orchestratord.seed.run_seed", boom)
        self._patch_uvicorn(monkeypatch)
        from orchestratord.cli.serve import run as serve_run

        assert serve_run(_make_serve_args(["--no-seed"])) == 0

    def test_skip_env_disables(self, isolated_env, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_SKIP_SEED", "1")

        def boom():
            raise AssertionError("run_seed must not be called when SKIP_SEED=1")

        monkeypatch.setattr("orchestratord.seed.run_seed", boom)
        self._patch_uvicorn(monkeypatch)
        from orchestratord.cli.serve import run as serve_run

        assert serve_run(_make_serve_args([])) == 0


def test_sha256_consistency_with_stdlib() -> None:
    payload = "one-time plaintext"
    assert hash_token(payload) == hashlib.sha256(payload.encode()).hexdigest()
