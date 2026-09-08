"""BackendRunner live-session exposure wiring (Phase A.2 follow-up).

Pins that ``_expose_live_session`` registers a ``LiveSession`` whose
``process_tree`` is a ``TurnProcessControl`` and mirrors the run as a
``sessions`` DB row under the ``daemon`` workspace, and that
``_retire_live_session`` drops the registry entry and finalizes the row
without clobbering an operator-set terminal status (``stopped``).

The runner writes through ``orchestratord.api.db``'s cached factory,
which defaults to the production DSN — these tests repoint that factory
at the ``orchestratord_test`` engine so the shared live-Postgres
isolation (per-test TRUNCATE) applies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.db import models as orm
from orchestratord.db.engine import build_session_factory
from orchestratord.db.repository import Repositories
from orchestratord.process_control import TurnProcessControl
from orchestratord.runtime import LiveSessionRegistry
from orchestratord.spi.capabilities import BackendCapabilities

pytestmark = pytest.mark.database


class _StubRunner(BackendRunner):
    """BackendRunner without __init__ — only ``registry`` is exercised."""

    def __init__(self) -> None:  # type: ignore[no-super-call]
        self.registry = LiveSessionRegistry()


class _StubSpiSession:
    session_id = "spi-stub"
    current_pid = None  # no live child process in unit tests


def _stub_agent_session(status: str = "running") -> SimpleNamespace:
    """Duck-typed AgentSession covering the surface the wiring touches."""
    return SimpleNamespace(
        control_socket=None,
        issue=SimpleNamespace(id="ccb-smoke-005"),
        run_id="20260908_000000_smoke",
        workspace=SimpleNamespace(path=Path("/tmp/orchestratord-ccb-test")),
        status=status,
    )


@pytest.fixture
def test_db_factory(db_engine, monkeypatch: pytest.MonkeyPatch):
    """Repoint the runner's DB seam at the orchestratord_test engine."""
    import orchestratord.api.db as api_db

    factory = build_session_factory(db_engine)
    monkeypatch.setattr(api_db, "_get_session_factory", lambda: factory)
    return factory


async def test_expose_registers_live_session_and_db_row(test_db_factory, db) -> None:
    runner = _StubRunner()
    session = _stub_agent_session()

    live_id = await runner._expose_live_session(
        session, _StubSpiSession(), BackendCapabilities()
    )

    assert live_id is not None
    live = await runner.registry.get(live_id)
    assert live is not None
    assert isinstance(live.process_tree, TurnProcessControl)
    assert live.metadata["issue_id"] == "ccb-smoke-005"
    assert live.metadata["run_id"] == "20260908_000000_smoke"

    repos = Repositories(db)
    row = await repos.sessions.get(UUID(live_id))
    assert row is not None
    assert row.status == "running"
    assert row.mode == "single"
    workspace = await repos.workspaces.by_slug("daemon")
    assert workspace is not None
    assert workspace.id == row.workspace_id


async def test_retire_finalizes_row_and_empties_registry(test_db_factory, db) -> None:
    runner = _StubRunner()
    session = _stub_agent_session()
    live_id = await runner._expose_live_session(
        session, _StubSpiSession(), BackendCapabilities()
    )
    assert live_id is not None

    session.status = "completed"
    await runner._retire_live_session(live_id, session)

    assert await runner.registry.get(live_id) is None
    row = await Repositories(db).sessions.get(UUID(live_id))
    assert row is not None
    assert row.status == "completed"


async def test_finalize_does_not_clobber_operator_stopped(test_db_factory, db) -> None:
    runner = _StubRunner()
    session = _stub_agent_session()
    live_id = await runner._expose_live_session(
        session, _StubSpiSession(), BackendCapabilities()
    )
    assert live_id is not None

    # The stop endpoint already marked the row terminal from the API side.
    repos = Repositories(db)
    row = await repos.sessions.get(UUID(live_id))
    assert row is not None
    row.status = "stopped"
    await db.commit()

    session.status = "failed"  # runner-side stop outcome
    await runner._finalize_session_row(live_id, session)

    row = await Repositories(db).sessions.get(UUID(live_id))
    assert row is not None
    assert row.status == "stopped"

    await runner._retire_live_session(live_id, session)


async def test_expose_survives_registry_failure(test_db_factory) -> None:
    runner = _StubRunner()

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("registry down")

    runner.registry.register = _boom  # type: ignore[method-assign]

    live_id = await runner._expose_live_session(
        _stub_agent_session(), _StubSpiSession(), BackendCapabilities()
    )
    assert live_id is None  # degraded, not crashed


async def test_expose_survives_db_failure(
    test_db_factory, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    import orchestratord.api.db as api_db

    def _boom_factory() -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(api_db, "_get_session_factory", _boom_factory)

    runner = _StubRunner()
    live_id = await runner._expose_live_session(
        _stub_agent_session(), _StubSpiSession(), BackendCapabilities()
    )
    # Registry half still succeeded — control remains available.
    assert live_id is not None
    assert await runner.registry.get(live_id) is not None


async def test_retire_with_none_id_is_noop() -> None:
    runner = _StubRunner()
    await runner._retire_live_session(None, _stub_agent_session())  # no raise
