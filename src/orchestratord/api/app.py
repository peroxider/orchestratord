"""FastAPI application factory and instance (``docs/FEATURE_GAP_VS_MULTICA.md``
§5.5.1).

The single ``app`` object is what ``uvicorn`` serves via ``orchestratord serve``
and what the contract tests import directly.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orchestratord.api.routers import (
    agents,
    audit,
    autopilots,
    channels,
    dashboard,
    inbox,
    integrations,
    issues,
    members,
    projects,
    realtime,
    runtimes,
    sessions,
    skills,
    squads,
    tokens,
    usage,
    vcs,
)


def create_app(
    backend_runner: object | None = None,
    *,
    enable_chat_daemon: bool | None = None,
    enable_autopilot: bool | None = None,
) -> FastAPI:
    """Build a FastAPI app, optionally wiring a :class:`BackendRunner`.

    ``backend_runner`` is exposed via :func:`orchestratord.api.runtime.
    set_backend_runner` so router handlers can forward operator decisions
    (approve/deny/pause/resume/stop) to the running in-process agent
    sessions. ``None`` (default) preserves the Phase-1 HTTP-only
    behaviour where the API is decoupled from the daemon.

    ``enable_chat_daemon`` starts the §6.1d chat dispatcher claim loop and
    ``enable_autopilot`` the §7.1 cron scheduler for the app's lifetime.
    Both default to their ``ORCHESTRATORD_*_DAEMON=1`` env flags; tests
    never set them, so they keep a daemon-free app.
    """
    from orchestratord.api.runtime import set_backend_runner

    set_backend_runner(backend_runner)  # type: ignore[arg-type]

    if enable_chat_daemon is None:
        enable_chat_daemon = os.environ.get("ORCHESTRATORD_CHAT_DAEMON") == "1"
    if enable_autopilot is None:
        enable_autopilot = os.environ.get("ORCHESTRATORD_AUTOPILOT_DAEMON") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        daemon = None
        scheduler = None
        if enable_chat_daemon:
            from orchestratord.chat_daemon import start_chat_daemon

            daemon = start_chat_daemon()
            app.state.chat_dispatcher = daemon
        if enable_autopilot:
            from orchestratord.db.engine import build_session_factory
            from orchestratord.scheduler.autopilot import AutopilotScheduler

            scheduler = AutopilotScheduler(build_session_factory())
            await scheduler.start()
            app.state.autopilot_scheduler = scheduler
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            if daemon is not None:
                from orchestratord.chat_daemon import stop_chat_daemon

                await stop_chat_daemon(daemon)

    application = FastAPI(
        title="orchestratord API",
        version="0.1.0",
        description=(
            "HTTP surface for the orchestratord daemon, workflow engine, SPI, "
            "and skills.  Does not bypass the orchestratord package to reach "
            "backend-private protocols (§3.1)."
        ),
        lifespan=lifespan,
    )
    # The Next.js console (apps/web) calls this API cross-origin from its own
    # port; the browser blocks every REST response without CORS (WebSocket
    # is exempt).  Origins stay env-overridable for non-default deployments.
    cors_env = os.environ.get("ORCHESTRATORD_CORS_ORIGINS", "")
    cors_origins = [o.strip() for o in cors_env.split(",") if o.strip()] or [
        "http://localhost:3100",
        "http://127.0.0.1:3100",
        "http://localhost:3000",
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(skills.router)
    application.include_router(dashboard.router)
    application.include_router(realtime.router)
    application.include_router(issues.router)
    application.include_router(agents.router)
    application.include_router(sessions.router)
    application.include_router(squads.router)
    application.include_router(projects.router)
    application.include_router(autopilots.router)
    application.include_router(runtimes.router)
    application.include_router(usage.router)
    application.include_router(inbox.router)
    application.include_router(channels.router)
    application.include_router(integrations.router)
    application.include_router(tokens.router)
    application.include_router(members.router)
    application.include_router(audit.router)
    application.include_router(vcs.router)
    return application


app = create_app()
