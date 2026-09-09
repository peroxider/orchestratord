"""Regression tests for #22: asyncpg connect must carry a short timeout.

Without the fix, ``asyncpg.connect`` (and the SQLAlchemy async engine over
asyncpg) falls back to asyncpg's 60s default connect timeout.  When port 5432
is black-holed (SYN dropped, no RST) every DB-touching fixture hangs 60s+
before skipping, and the daemon logs a full TimeoutError stack per run.

These tests pin:
- every test-conftest ``asyncpg.connect`` receives ``timeout=3`` (or a value
  ≤ 3) so the skip path completes in <5s instead of 60s+
- ``build_engine`` forwards ``connect_args={"timeout": 3}`` to
  ``create_async_engine`` (the production SQLAlchemy engine path)
- a DB-down ``_create_session_row`` degrades to one concise WARNING
  (fix guidance, no full stack trace) instead of ``logger.exception``
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import LogCaptureFixture

from orchestratord.backend_runner import BackendRunner
from orchestratord.db.engine import build_engine
from orchestratord.runtime import LiveSessionRegistry
from orchestratord.spi.capabilities import BackendCapabilities

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# All three conftest files share the same ``_ensure_test_db`` that calls
# ``asyncpg.connect(_ADMIN_DSN)`` — parametrize over them.
_CONFTEST_PATHS: list[Path] = [
    Path(__file__).parent / "api" / "conftest.py",
    Path(__file__).parent / "scheduler" / "conftest.py",
    Path(__file__).parent / "db_integration" / "conftest.py",
]


def _load_conftest(path: Path):
    """Import a conftest module by file path (they are not proper packages)."""
    module_name = f"_cf_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Pre-populate sys.modules so the module's own relative/absolute imports
    # resolve correctly — the module already imports from ``orchestratord.*``.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _StubRunner(BackendRunner):
    """BackendRunner without __init__ — only ``registry`` is exercised."""

    def __init__(self) -> None:  # type: ignore[no-super-call]
        self.registry = LiveSessionRegistry()


class _StubSpiSession:
    session_id = "spi-stub"
    current_pid = None


def _stub_agent_session(status: str = "running") -> SimpleNamespace:
    """Duck-typed AgentSession covering the surface the wiring touches."""
    return SimpleNamespace(
        control_socket=None,
        issue=SimpleNamespace(id="ccb-smoke-005"),
        run_id="20260908_000000_smoke",
        workspace=SimpleNamespace(path=Path("/tmp/orchestratord-ccb-test")),
        status=status,
    )


# ---------------------------------------------------------------------------
# 1.  Test-conftest asyncpg.connect receives timeout=3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conftest_path",
    _CONFTEST_PATHS,
    ids=lambda p: p.parent.name,
)
async def test_conftest_asyncpg_connect_passes_short_timeout(
    conftest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conftest ``_ensure_test_db`` passes ``timeout`` to ``asyncpg.connect``.

    Without the fix the ``timeout`` kwarg is absent → the captured kwargs
    dict lacks the key, and the assertion below fails (red).
    With the fix it is ``3`` → the assertion passes (green).
    """
    conftest = _load_conftest(conftest_path)
    captured: dict = {}

    async def _stub_connect(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        raise OSError("black-holed 5432 — stub raises immediately")

    monkeypatch.setattr("asyncpg.connect", _stub_connect)

    start = time.monotonic()
    with pytest.raises(OSError):
        await conftest._ensure_test_db()
    elapsed = time.monotonic() - start

    # 1a. The timeout kwarg was passed (the core regression assertion).
    assert "timeout" in captured, (
        f"asyncpg.connect called without timeout kwarg; "
        f"captured kwargs: {captured}"
    )
    assert captured["timeout"] <= 3, (
        f"timeout={captured['timeout']} exceeds 3s"
    )

    # 1b. The whole failure path completes in <5s (60s would be the
    #     old default that made the suite hour-scale).
    assert elapsed < 5, (
        f"_ensure_test_db took {elapsed:.2f}s — fast skip broken? "
        f"captured kwargs: {captured}"
    )


# ---------------------------------------------------------------------------
# 2.  Production engine: build_engine forwards connect_args timeout
# ---------------------------------------------------------------------------


def test_build_engine_forwards_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_engine`` passes ``connect_args={"timeout": 3}`` to asyncpg.

    This is the production path — every session factory, ``create_schema``
    call, and ``_get_session_factory`` dependency ultimately goes through
    this function.
    """
    captured: dict = {}

    def _stub_create_async_engine(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured["connect_args"] = kwargs.get("connect_args")
        return object()

    monkeypatch.setattr(
        "orchestratord.db.engine.create_async_engine", _stub_create_async_engine
    )

    test_url = "postgresql+asyncpg://x:y@127.0.0.1:5432/db"
    build_engine(test_url)

    assert captured["connect_args"] == {"timeout": 3}, (
        f"expected connect_args={{'timeout': 3}}, got {captured['connect_args']}"
    )


# ---------------------------------------------------------------------------
# 3.  Daemon log noise: DB failure → concise WARNING (no full stack trace)
# ---------------------------------------------------------------------------


async def test_session_row_db_failure_logs_concise_warning(
    caplog: LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_create_session_row`` fails, the daemon emits one concise
    WARNING with a fix hint, not a ``logger.exception`` full stack trace.

    This eliminates the per-run TimeoutError stack trace noise from the
    daemon log when PostgreSQL is unreachable.
    """
    runner = _StubRunner()
    session = _stub_agent_session()

    async def _boom(live_id: str, session: object) -> None:
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(runner, "_create_session_row", _boom)

    with caplog.at_level(logging.WARNING, logger="orchestratord.backend_runner"):
        live_id = await runner._expose_live_session(
            session, _StubSpiSession(), BackendCapabilities()
        )

    # The registry half still succeeds — only the DB row half degrades.
    assert live_id is not None

    # Filter down to the session-row-creation warning.
    session_records = [
        r for r in caplog.records if "sessions DB row creation failed" in r.message
    ]
    assert len(session_records) == 1, (
        f"expected exactly one session-row warning, got {len(session_records)}"
    )
    record = session_records[0]

    # 3a. Log level is WARNING (not ERROR).
    assert record.levelno == logging.WARNING, (
        f"expected WARNING level, got {logging.getLevelName(record.levelno)}"
    )

    # 3b. No full stack trace attached (``logger.exception`` auto-includes
    #     ``exc_info=True`` and formats the full traceback; ``logger.warning``
    #     without ``exc_info`` does not).
    assert record.exc_info is None or record.exc_info == (None, None, None), (
        "exc_info is set — a full stack trace would be printed; "
        "use logger.warning without exc_info"
    )
    assert record.exc_text is None, (
        "exc_text is set — a full stack trace was formatted"
    )

    # 3c. The fix hint is present.
    assert "ORCHESTRATORD_DATABASE_URL" in record.message, (
        "fix-hint env var name missing from warning message"
    )