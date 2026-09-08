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
        default="127.0.0.1",
        help=(
            "Bind address (default: 127.0.0.1). Exposing the operator console "
            "on a non-loopback interface requires a trusted VPN or an "
            "authenticating reverse proxy."
        ),
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
    serve_parser.add_argument(
        "--no-chat-daemon",
        action="store_true",
        help="Disable the §6.1d chat dispatcher claim loop",
    )
    serve_parser.add_argument(
        "--no-autopilot",
        action="store_true",
        help="Disable the §7.1 autopilot cron scheduler",
    )
    serve_parser.add_argument(
        "--with-web",
        action="store_true",
        help=(
            "Also launch the Next.js Web client (apps/web) alongside the "
            "API; terminated when the API exits (§10.1)"
        ),
    )
    serve_parser.add_argument(
        "--web-port",
        type=int,
        default=3100,
        help="Port for the spawned Next.js client with --with-web (default: 3100)",
    )
    serve_parser.add_argument(
        "--web-dev",
        action="store_true",
        help="Run the spawned Next.js client in dev mode",
    )
    serve_parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Skip the §10.2 first-boot default-workspace seed",
    )


def run(args: argparse.Namespace) -> int:
    import os
    import uvicorn

    if args.no_chat_daemon:
        os.environ.pop("ORCHESTRATORD_CHAT_DAEMON", None)
    else:
        os.environ.setdefault("ORCHESTRATORD_CHAT_DAEMON", "1")
    if args.no_autopilot:
        os.environ.pop("ORCHESTRATORD_AUTOPILOT_DAEMON", None)
    else:
        os.environ.setdefault("ORCHESTRATORD_AUTOPILOT_DAEMON", "1")

    # §10.2 first-boot seed — default workspace + fixed owner member +
    # one daemon runtime token. Plaintext is printed exactly once here.
    if not getattr(args, "no_seed", False) and (
        os.environ.get("ORCHESTRATORD_SKIP_SEED") != "1"
    ):
        from orchestratord.seed import run_seed

        seeded = run_seed()
        if seeded is not None:
            print(
                "orchestratord serve: seeded default workspace "
                f"{seeded.workspace_id} (owner member {seeded.member_id}). "
                "Daemon runtime token (shown ONCE, only its hash is "
                f"stored): {seeded.token_plaintext}"
            )

    web_proc = None
    if getattr(args, "with_web", False):
        from orchestratord.cli import web as web_cli

        web_proc = web_cli.launch_web_process(port=args.web_port, dev=args.web_dev)
    try:
        uvicorn.run(
            "orchestratord.api.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
    finally:
        if web_proc is not None:
            web_proc.terminate()
    return 0
