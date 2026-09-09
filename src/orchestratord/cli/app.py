"""Business application namespace.

P5（DESIGN §6 :420/:431）：``app`` 命令组与 ``app list`` 由 applications
注册表驱动——新增应用只需注册 :class:`ApplicationSpec`，无需改本文件。
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestratord.applications import ApplicationSpec


def add_app_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("app", help="Run business applications built on the engine")
    commands = parser.add_subparsers(dest="app_subcommand", required=True)
    commands.add_parser("list", help="List bundled business applications")
    from orchestratord.applications import application_specs

    for spec in application_specs():
        app_parser = commands.add_parser(spec.cli_name, help=spec.description)
        app_parser.add_argument("arguments", nargs=argparse.REMAINDER)


def _delegate(spec: ApplicationSpec, argv: list[str]) -> int:
    if not argv:
        print(
            f"usage: orchestratord app {spec.cli_name} <serve|issue-command> ...",
            file=sys.stderr,
        )
        return 2
    if argv[0] == "serve":
        from orchestratord.cli.server import add_server_parser, run

        parser = argparse.ArgumentParser(prog="orchestratord app")
        subs = parser.add_subparsers(dest="top", required=True)
        add_server_parser(
            subs, command_name=spec.cli_name, start_command="serve", application=spec.name
        )
        args = parser.parse_args([spec.cli_name, "serve", *argv[1:]])
        return run(args)
    from orchestratord.cli.issue import add_issue_parser, run

    parser = argparse.ArgumentParser(prog="orchestratord app")
    subs = parser.add_subparsers(dest="top", required=True)
    add_issue_parser(subs, command_name=spec.cli_name)
    args = parser.parse_args([spec.cli_name, *argv])
    return run(args)


def run(args: argparse.Namespace) -> int:
    from orchestratord.applications import application_specs

    if args.app_subcommand == "list":
        for spec in application_specs():
            print(f"{spec.cli_name}\t{spec.description}")
        return 0
    for spec in application_specs():
        if args.app_subcommand == spec.cli_name:
            return _delegate(spec, args.arguments)
    return 2
