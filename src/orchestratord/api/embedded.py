"""Embedded uvicorn server for ``server start --serve-api``.

Runs the FastAPI HTTP surface (``orchestratord.api.app.create_app``)
inside the orchestration daemon's asyncio loop so the API and the
orchestrator share one process — and one
:class:`~orchestratord.backend_runner.BackendRunner` instance. Router
handlers (sessions approve/deny/pause/resume/stop) then operate on the
very runner executing the issues, instead of degrading to DB-only mode.
"""

from __future__ import annotations

import contextlib
import logging

import uvicorn

logger = logging.getLogger(__name__)


class EmbeddedUvicornServer(uvicorn.Server):
    """uvicorn server that never installs its own signal handlers.

    The daemon owns SIGTERM/SIGINT — it registers handlers on the running
    loop for graceful ``subsystem.shutdown()``. uvicorn's ``serve()``
    wraps itself in ``capture_signals()``, which would replace those
    handlers for the whole process: a bare ``kill``/Ctrl+C would only
    stop the HTTP server and never reach the orchestrator. With capture
    neutralized, shutdown of the embedded server is driven externally by
    setting ``should_exit`` once the orchestrator run loop returns.
    """

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


def build_embedded_server(
    backend_runner: object,
    port: int,
) -> EmbeddedUvicornServer:
    """Create the embedded API server wired to ``backend_runner``.

    ``create_app`` installs the runner process-wide
    (``orchestratord.api.runtime.set_backend_runner``) as a side effect,
    so building the server is sufficient to complete the sharing wiring.
    The server only binds loopback: this surface forwards operator
    decisions into the running daemon and is not meant to be exposed.
    """
    from orchestratord.api.app import create_app

    config = uvicorn.Config(
        create_app(backend_runner=backend_runner),
        host="127.0.0.1",
        port=port,
        access_log=False,
    )
    return EmbeddedUvicornServer(config)


async def serve_contained(server: uvicorn.Server) -> None:
    """Run ``server.serve()`` containing uvicorn's startup ``sys.exit``.

    On startup failure (e.g. the port is already bound) uvicorn calls
    ``sys.exit(STARTUP_FAILURE)`` from inside the serve coroutine. A
    ``SystemExit`` escaping into an asyncio task is re-raised by the loop
    runner and tears down the whole process — the orchestrator daemon
    would die with exit 3 and no traceback over an auxiliary HTTP
    surface. Swallow it here: the daemon keeps orchestrating, and
    callers detect the dead surface via ``server.started is False``.
    """
    try:
        await server.serve()
    except SystemExit as exc:
        logger.error(
            "Embedded API server failed to start (%s) — continuing "
            "without the HTTP surface",
            exc,
        )
