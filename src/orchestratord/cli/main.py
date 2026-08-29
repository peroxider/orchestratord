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


def app() -> None:
    """Entry point for ``orchestratord`` console_script."""
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
    from orchestratord.cli.dashboard import add_dashboard_parser
    from orchestratord.cli.issue import add_issue_parser
    from orchestratord.cli.rules import add_rules_parser
    from orchestratord.cli.server import add_server_parser
    from orchestratord.cli.skills import add_skills_parser
    from orchestratord.cli.workflow import add_workflow_parser
    from orchestratord.cli.workspace import add_workspace_parser

    add_server_parser(subparsers)
    add_issue_parser(subparsers)
    add_workflow_parser(subparsers)
    add_dashboard_parser(subparsers)
    add_rules_parser(subparsers)
    add_workspace_parser(subparsers)
    add_skills_parser(subparsers)

    args = parser.parse_args()

    # Dispatch to the appropriate run() function.
    subcommand = args.subcommand
    if subcommand == "server":
        from orchestratord.cli.server import run
    elif subcommand == "issue":
        from orchestratord.cli.issue import run
    elif subcommand == "workflow":
        from orchestratord.cli.workflow import run
    elif subcommand == "dashboard":
        from orchestratord.cli.dashboard import run
    elif subcommand == "rules":
        from orchestratord.cli.rules import run
    elif subcommand == "workspace":
        from orchestratord.cli.workspace import run
    elif subcommand == "skills":
        from orchestratord.cli.skills import run
    else:
        parser.print_help()
        sys.exit(2)

    sys.exit(run(args))