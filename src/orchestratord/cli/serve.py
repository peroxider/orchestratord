"""``orchestratord serve`` — launch the FastAPI Web backend.

Phase 0 (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.5.1) splits the monolithic
``dashboard`` LiveView into a FastAPI app (``orchestratord.api.app``) plus
domain routers. This command is the production launcher: it runs that ASGI
app under uvicorn so the Next.js Web client (``apps/web``) and the daemon-side
runtime can reach the HTTP surface.
"""

from __future__ import annotations

import argparse


def add_serve_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``serve`` subcommand."""
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the FastAPI Web backend (uvicorn)",
        description=(
            "Serve the orchestratord HTTP surface (orchestratord.api.app) "
            "under uvicorn."
        ),
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Bind port (default: 9000)",
    )
    serve_parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload for development",
    )


def run(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "orchestratord.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0
