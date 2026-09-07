"""orchestratord CLI entry point.

Wire the noun-verb subcommand modules (server, issue, workflow, dashboard,
rules, workspace) into a single top-level argparse parser and expose
``app`` as the console_scripts callable.

Usage::

    orchestratord server start --workflow ...
    orchestratord issue list
    orchestratord workflow init
    orchestratord dashboard --port 8080
"""

from __future__ import annotations

import argparse
import sys

from orchestratord._version import __version__


def _configure_stdio() -> None:
    """Use UTF-8 for the cross-platform CLI without failing on Windows GBK."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def app() -> None:
    """Entry point for ``orchestratord`` console_script."""
    _configure_stdio()
    parser = argparse.ArgumentParser(
        prog="orchestratord",
        description="Agent-agnostic orchestration daemon — multi-backend workflow engine",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"orchestratord {__version__}",
    )

    subparsers = parser.add_subparsers(
        dest="subcommand",
        required=True,
    )

    # Register subcommand parsers from each CLI module.
    from orchestratord.cli.app import add_app_parser
    from orchestratord.cli.backend import add_backend_parser
    from orchestratord.cli.dashboard import add_dashboard_parser
    from orchestratord.cli.db import add_db_parser
    from orchestratord.cli.issue import add_issue_parser
    from orchestratord.cli.rules import add_rules_parser
    from orchestratord.cli.run import add_run_parser
    from orchestratord.cli.serve import add_serve_parser
    from orchestratord.cli.server import add_server_parser
    from orchestratord.cli.skills import add_skills_parser
    from orchestratord.cli.web import add_web_parser
    from orchestratord.cli.workflow import add_workflow_parser
    from orchestratord.cli.workspace import add_workspace_parser

    add_server_parser(subparsers)
    add_server_parser(subparsers, command_name="daemon", dest="daemon_subcommand")
    add_run_parser(subparsers)
    add_backend_parser(subparsers)
    add_app_parser(subparsers)
    add_issue_parser(subparsers)
    add_workflow_parser(subparsers)
    add_dashboard_parser(subparsers)
    add_serve_parser(subparsers)
    add_web_parser(subparsers)
    add_db_parser(subparsers)
    add_rules_parser(subparsers)
    add_workspace_parser(subparsers)
    add_skills_parser(subparsers)

    args = parser.parse_args()

    # Dispatch to the appropriate run() function.
    subcommand = args.subcommand
    if subcommand in ("server", "daemon"):
        from orchestratord.cli.server import run
    elif subcommand == "run":
        from orchestratord.cli.run import run
    elif subcommand == "backend":
        from orchestratord.cli.backend import run
    elif subcommand == "app":
        from orchestratord.cli.app import run
    elif subcommand == "issue":
        from orchestratord.cli.issue import run
    elif subcommand == "workflow":
        from orchestratord.cli.workflow import run
    elif subcommand == "dashboard":
        from orchestratord.cli.dashboard import run
    elif subcommand == "serve":
        from orchestratord.cli.serve import run
    elif subcommand == "web":
        from orchestratord.cli.web import run
    elif subcommand == "db":
        from orchestratord.cli.db import run
    elif subcommand == "rules":
        from orchestratord.cli.rules import run
    elif subcommand == "workspace":
        from orchestratord.cli.workspace import run
    elif subcommand == "skills":
        from orchestratord.cli.skills import run
    else:
        parser.print_help()
        sys.exit(2)

    try:
        code = run(args)
    except BrokenPipeError:
        # The consumer closed the pipe early (e.g. `orchestratord run
        # logs … | head`). That is a normal termination for a print-heavy
        # read-only command: exit 0 without a traceback. Point stdout at
        # devnull first so the interpreter's final flush cannot raise
        # EPIPE again on shutdown.
        import os

        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        # Ctrl+C aborts interactive prompts and long operations cleanly
        # (128 + SIGINT = 130) instead of dumping a traceback.
        print("\n✗ Interrupted.", file=sys.stderr)
        sys.exit(130)
    sys.exit(code)
