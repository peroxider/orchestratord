"""Process-wide :class:`BackendRunner` accessor for the API layer.

The sessions router (``orchestratord.api.routers.sessions``) needs to
forward operator decisions (approve/deny/pause/resume/stop) to the
running :class:`orchestratord.spi.session.AgentSession`. In production
the CLI builds one ``BackendRunner`` and serves both ``orchestratord
serve`` (HTTP) and ``orchestratord run`` (CLI) from the same process;
this accessor exposes that single instance to the FastAPI dependency
graph so router handlers can call ``runner.registry.get(session_id)``.

``set_backend_runner`` is called by the CLI at boot
(``orchestratord.cli.serve`` / ``orchestratord.cli.run``). The module
global defaults to ``None`` so production ``orchestratord serve`` (HTTP
without an attached daemon) and the test client (which never sets a
runner) degrade to the Phase-1 DB-only behaviour without crashing.

Mirrors the pattern used by :mod:`orchestratord.api.realtime`
(``get_broker``) and :mod:`orchestratord.api.state``
(``get_dashboard_state``).
"""

from __future__ import annotations

from orchestratord.backend_runner import BackendRunner

_runner: BackendRunner | None = None


def get_backend_runner() -> BackendRunner | None:
    """Return the process-wide :class:`BackendRunner`, or ``None``.

    ``None`` is a valid value: it means the API is serving in HTTP-only
    mode without an attached daemon. Routers must handle this gracefully
    (e.g. fall back to DB-only state, or return 503 for in-process ops).
    """
    return _runner


def set_backend_runner(runner: BackendRunner | None) -> None:
    """Install the process-wide :class:`BackendRunner`.

    Pass ``None`` to clear (used by ``reset_backend_runner`` and the CLI
    during shutdown).
    """
    global _runner
    _runner = runner


def reset_backend_runner() -> None:
    """Drop the process-wide :class:`BackendRunner` (test helper)."""
    set_backend_runner(None)


# ---------------------------------------------------------------------------
# Embedded API port disclosure
# ---------------------------------------------------------------------------

_api_port: int | None = None


def get_api_port() -> int | None:
    """Return the port of the API server embedded in this process, or ``None``.

    Set only by ``server start --serve-api`` (same-process daemon + API);
    standalone ``orchestratord serve`` runs in its own process and never
    sets it. The orchestrator reads it in ``_metadata_extras`` so the
    heartbeat-persisted metadata discloses the HTTP surface to
    ``server status``.
    """
    return _api_port


def set_api_port(port: int | None) -> None:
    """Record the embedded API port (``None`` to clear — test helper)."""
    global _api_port
    _api_port = port
