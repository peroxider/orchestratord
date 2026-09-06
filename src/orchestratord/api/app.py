"""FastAPI application factory and instance (``docs/FEATURE_GAP_VS_MULTICA.md``
§5.5.1).

The single ``app`` object is what ``uvicorn`` serves via ``orchestratord serve``
and what the contract tests import directly.
"""

from __future__ import annotations

from fastapi import FastAPI

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


def create_app() -> FastAPI:
    application = FastAPI(
        title="orchestratord API",
        version="0.1.0",
        description=(
            "HTTP surface for the orchestratord daemon, workflow engine, SPI, "
            "and skills.  Does not bypass the orchestratord package to reach "
            "backend-private protocols (§3.1)."
        ),
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
