"""Backend discovery and diagnostics commands."""

from __future__ import annotations

import argparse
import json
import sys


def add_backend_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("backend", help="Inspect execution backends")
    commands = parser.add_subparsers(dest="backend_subcommand", required=True)
    commands.add_parser("list", help="List installed backend descriptors")
    inspect_parser = commands.add_parser("inspect", help="Show one backend descriptor")
    inspect_parser.add_argument("name")
    doctor_parser = commands.add_parser("doctor", help="Resolve and validate a backend")
    doctor_parser.add_argument("name")


def run(args: argparse.Namespace) -> int:
    from orchestratord.backend_registry import (
        BackendNotFoundError,
        discover_descriptors,
        list_backends,
        resolve_backend,
    )

    command = args.backend_subcommand
    if command == "list":
        rows = list_backends()
        print(f"{'NAME':<22} {'FAMILY':<12} PACKAGE")
        for row in rows:
            print(f"{row['name']:<22} {row['family']:<12} {row['backend_package']}")
        return 0

    descriptor = discover_descriptors().get(args.name)
    if descriptor is None:
        print(f"backend {args.name!r} is not installed", file=sys.stderr)
        return 1

    if command == "inspect":
        print(json.dumps({
            "name": descriptor.name,
            "display_name": descriptor.display_name,
            "family": descriptor.family.value,
            "backend_package": descriptor.backend_package,
            "capabilities": sorted(descriptor.capabilities),
            "cli_command": descriptor.cli_command,
            "model_discovery": descriptor.model_discovery,
        }, indent=2))
        return 0

    if command == "doctor":
        try:
            backend = resolve_backend(args.name, strict=True)
        except (BackendNotFoundError, RuntimeError) as exc:
            print(f"backend check failed: {exc}", file=sys.stderr)
            return 1
        print(f"backend {args.name!r} is ready ({backend.display_name})")
        return 0

    return 2
