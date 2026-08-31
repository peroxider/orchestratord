"""Business application namespace."""

from __future__ import annotations

import argparse
import sys


def add_app_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("app", help="Run business applications built on the engine")
    commands = parser.add_subparsers(dest="app_subcommand", required=True)
    commands.add_parser("list", help="List bundled business applications")
    issue_pr = commands.add_parser("issue-pr", help="Issue-to-PR application commands")
    issue_pr.add_argument("arguments", nargs=argparse.REMAINDER)


def _delegate(argv: list[str]) -> int:
    if not argv:
        print("usage: orchestratord app issue-pr <serve|issue-command> ...", file=sys.stderr)
        return 2
    if argv[0] == "serve":
        from orchestratord.cli.server import add_server_parser, run
        parser = argparse.ArgumentParser(prog="orchestratord app")
        subs = parser.add_subparsers(dest="top", required=True)
        add_server_parser(subs, command_name="issue-pr", start_command="serve")
        args = parser.parse_args(["issue-pr", "serve", *argv[1:]])
        return run(args)
    from orchestratord.cli.issue import add_issue_parser, run
    parser = argparse.ArgumentParser(prog="orchestratord app")
    subs = parser.add_subparsers(dest="top", required=True)
    add_issue_parser(subs, command_name="issue-pr")
    args = parser.parse_args(["issue-pr", *argv])
    return run(args)


def run(args: argparse.Namespace) -> int:
    if args.app_subcommand == "list":
        print("issue-pr\tPoll tracker issues, run coding workflows, and synchronize pull requests")
        return 0
    if args.app_subcommand == "issue-pr":
        return _delegate(args.arguments)
    return 2
