"""Embedded same-process API server (``server start --serve-api``).

Covers the BackendRunner-sharing wiring: the embedded uvicorn server
never steals SIGTERM/SIGINT from the daemon, the app is built with the
orchestrator's runner installed process-wide, and the API port survives
into metadata.json (heartbeat-safe) so ``server status`` discloses it.
"""

from __future__ import annotations

import argparse
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.api.embedded import EmbeddedUvicornServer, build_embedded_server
from orchestratord.api.runtime import (
    get_api_port,
    get_backend_runner,
    reset_backend_runner,
    set_api_port,
)
from orchestratord.workspace_locator import write_orchestrator_metadata


@pytest.fixture(autouse=True)
def _clean_process_globals() -> None:
    """Keep the module-global runner/port from leaking between tests."""
    reset_backend_runner()
    set_api_port(None)
    yield
    reset_backend_runner()
    set_api_port(None)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    import orchestratord.cli.server as server_cli
    import orchestratord.workspace_locator as locator

    metadata_root = home / ".orchestratord" / "orchestrator"
    monkeypatch.setattr(locator, "ORCHESTRATORD_ORCHESTRATOR_DIR", metadata_root)
    monkeypatch.setattr(server_cli, "ORCHESTRATORD_ORCHESTRATOR_DIR", metadata_root)
    repo = tmp_path / "repo"
    (repo / "workspace").mkdir(parents=True)
    monkeypatch.chdir(repo)
    return repo


# ---------------------------------------------------------------------------
# Embedded server signal neutrality
# ---------------------------------------------------------------------------


async def _dummy_app(scope, receive, send) -> None:  # pragma: no cover
    raise AssertionError("the embedded server is never actually served in tests")


def test_capture_signals_leaves_handlers_alone() -> None:
    """serve() must not replace the daemon's SIGTERM/SIGINT handlers."""
    import uvicorn

    server = EmbeddedUvicornServer(
        uvicorn.Config(_dummy_app, host="127.0.0.1", port=0)
    )
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    with server.capture_signals():
        after = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    assert before == after


def test_build_embedded_server_installs_runner() -> None:
    """create_app side effect: the runner is reachable process-wide."""
    runner = SimpleNamespace(backend_name="claude")
    server = build_embedded_server(runner, port=9111)

    assert isinstance(server, EmbeddedUvicornServer)
    assert get_backend_runner() is runner
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 9111


def test_serve_contained_swallows_startup_system_exit() -> None:
    """uvicorn sys.exits (STARTUP_FAILURE) inside the serve task on a bind
    failure; uncontained, that SystemExit escapes asyncio.run and kills
    the whole daemon. serve_contained must absorb it.
    """
    import asyncio

    from orchestratord.api.embedded import serve_contained

    class _StartupFailureServer:
        started = False
        should_exit = False

        async def serve(self) -> None:
            raise SystemExit(3)

    # Would raise SystemExit (killing pytest) if uncontained.
    asyncio.run(serve_contained(_StartupFailureServer()))


def test_serve_contained_passes_through_normal_serve() -> None:
    import asyncio

    from orchestratord.api.embedded import serve_contained

    class _GoodServer:
        started = False
        should_exit = False

        async def serve(self) -> None:
            self.started = True

    server = _GoodServer()
    asyncio.run(serve_contained(server))
    assert server.started is True


# ---------------------------------------------------------------------------
# API port accessors + metadata disclosure
# ---------------------------------------------------------------------------


def test_api_port_defaults_to_none_and_round_trips() -> None:
    assert get_api_port() is None
    set_api_port(9000)
    assert get_api_port() == 9000
    set_api_port(None)
    assert get_api_port() is None


def test_metadata_writer_persists_api_port(project: Path) -> None:
    import json

    md = write_orchestrator_metadata("workspace", api_port=9000)

    data = json.loads(md.read_text(encoding="utf-8"))
    assert data["api_port"] == 9000


def test_metadata_writer_omits_api_port_for_legacy_callers(project: Path) -> None:
    import json

    md = write_orchestrator_metadata("workspace")

    data = json.loads(md.read_text(encoding="utf-8"))
    assert "api_port" not in data


def test_runtime_lines_renders_api_line_and_legacy_absence() -> None:
    from orchestratord.cli.server import _runtime_lines

    lines = _runtime_lines({"api_port": 9000})
    assert lines == ["  API            : http://127.0.0.1:9000"]

    # Legacy metadata (no embedded API) renders no API line.
    assert _runtime_lines({"backend": "claude"}) == ["  Backend        : claude"]


def test_metadata_extras_includes_api_port_when_set() -> None:
    """The heartbeat path must carry the port or it would be wiped."""
    from orchestratord.orchestrator import Orchestrator

    fake = SimpleNamespace(
        workflow=SimpleNamespace(
            agent=SimpleNamespace(
                provider="anthropic",
                model="MiniMax-M3",
                permission_mode="bypassPermissions",
                max_concurrent_agents=2,
            ),
            sandbox=SimpleNamespace(approval_policy={"reject": {}}),
            polling=SimpleNamespace(interval_ms=5000),
        ),
        agent_runner=SimpleNamespace(backend_name="claude"),
        _backend=SimpleNamespace(name="claude"),
    )

    set_api_port(9111)
    extras = Orchestrator._metadata_extras(fake)
    assert extras["api_port"] == 9111
    assert extras["backend_name"] == "claude"

    set_api_port(None)
    extras = Orchestrator._metadata_extras(fake)
    assert "api_port" not in extras


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_start_parser_accepts_serve_api_flags() -> None:
    from orchestratord.cli.server import add_server_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top")
    add_server_parser(subparsers)

    with_api = parser.parse_args(
        ["server", "start", "--workflow", "w.md", "--serve-api", "--backend", "claude"]
    )
    assert with_api.serve_api is True
    assert with_api.api_port is None

    with_port = parser.parse_args(
        [
            "server",
            "start",
            "--workflow",
            "w.md",
            "--serve-api",
            "--api-port",
            "9111",
            "--backend",
            "claude",
        ]
    )
    assert with_port.api_port == 9111

    without = parser.parse_args(["server", "start", "--workflow", "w.md", "--backend", "claude"])
    assert without.serve_api is False
    assert without.api_port is None
