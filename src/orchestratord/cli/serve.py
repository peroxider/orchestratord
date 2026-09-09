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
    serve_parser.add_argument(
        "--peer-listen",
        type=str,
        default="127.0.0.1:9001",
        help=(
            "Peer federation listener as HOST:PORT (DESIGN §6.2; "
            "default: 127.0.0.1:9001)"
        ),
    )
    serve_parser.add_argument(
        "--peer-frame-listen",
        type=str,
        default=None,
        help=(
            "Optional peer/1 frame listener as HOST:PORT (PR-B2.1). "
            "When set, the Agent Card advertises the frame transport "
            "on this socket instead of the REST listener's "
            "/peer/v1/stream path. Default: unset — single-port "
            "design shares the REST listener."
        ),
    )
    serve_parser.add_argument(
        "--redis-url",
        type=str,
        default="redis://localhost:6379/0",
        help=(
            "Redis URL for peer event fan-out (DESIGN §7 R9; "
            "default: redis://localhost:6379/0)"
        ),
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
    # Peer Federation (DESIGN §10 PR4): the peer listener binding and
    # Redis URL default via flags but stay env-overridable for PR6's
    # two-daemon integration runs.
    os.environ.setdefault("ORCHESTRATORD_PEER_LISTEN", args.peer_listen)
    # PR-B2.1: optional split-port frame listener. Only set the env
    # when the operator opted in via the flag — leaving it unset
    # preserves the single-port design (frame shares the REST listener).
    peer_frame_listen = getattr(args, "peer_frame_listen", None)
    if peer_frame_listen:
        os.environ.setdefault("ORCHESTRATORD_PEER_FRAME_LISTEN", peer_frame_listen)
    os.environ.setdefault("ORCHESTRATORD_REDIS_URL", args.redis_url)

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
        config = uvicorn.Config(
            "orchestratord.api.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )

        class _PeerDrainingServer(uvicorn.Server):
            """D24: GOODBYE every connected peer + 5s drain on shutdown.

            uvicorn's graceful shutdown (SIGTERM and SIGINT both land in
            ``Server.shutdown``) first gives outbound peer sessions their
            §6.2 drain window, so in-flight INVOKEs finish and the remote
            side receives an explicit GOODBYE instead of a dead socket.
            """

            async def shutdown(self, sockets=None):
                from orchestratord.peer.connections import (
                    shutdown_peer_connections,
                )

                drained = await shutdown_peer_connections(drain_seconds=5.0)
                if drained:
                    print(
                        f"orchestratord serve: sent GOODBYE to {drained} "
                        "peer connection(s) after drain"
                    )
                await super().shutdown(sockets)

        _PeerDrainingServer(config).run()
    finally:
        if web_proc is not None:
            web_proc.terminate()
    return 0
