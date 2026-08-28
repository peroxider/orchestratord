"""``orchestratord skills`` subcommands — operator-facing skill management.

Subcommands:

* ``skills list``          — name / stale status / description table
* ``skills show <name>``   — full SKILL.md + source-map
* ``skills verify``        — re-run source-map validation; exit 1 if stale

Per DESIGN_agent_callable_skills.md §4 (Scheme D).
"""

from __future__ import annotations

import argparse
import sys

from orchestratord.skills.loader import load_all_skills


def add_skills_parser(subparsers: argparse._SubParsersAction) -> None:
    skills_parser = subparsers.add_parser(
        "skills",
        help="Inspect builtin agent-callable skills",
        description="List skills, show one in full, or verify source-map references.",
    )
    skills_sub = skills_parser.add_subparsers(dest="skills_subcommand", required=True)
    skills_sub.add_parser("list", help="List all skills with stale status")
    show_parser = skills_sub.add_parser("show", help="Show full SKILL.md and source-map")
    show_parser.add_argument("name", type=str, help="Skill name (kebab-case)")
    skills_sub.add_parser("verify", help="Verify source-map hashes; exit 1 if stale")


def run(args: argparse.Namespace) -> int:
    if args.skills_subcommand == "list":
        return _run_list()
    elif args.skills_subcommand == "show":
        return _run_show(args.name)
    elif args.skills_subcommand == "verify":
        return _run_verify()
    print(f"Unknown skills subcommand: {args.skills_subcommand}", file=sys.stderr)
    return 2


def _run_list() -> int:
    skills = load_all_skills()
    print(f"{'NAME':<24} {'STALE':<6} DESCRIPTION")
    for skill in skills:
        stale_marker = "YES" if skill.is_stale else "no"
        print(f"{skill.name:<24} {stale_marker:<6} {skill.description}")
    return 0


def _run_show(name: str) -> int:
    skills = {skill.name: skill for skill in load_all_skills()}
    skill = skills.get(name)
    if skill is None:
        print(f"skill {name!r} not found", file=sys.stderr)
        return 1
    print(f"--- SKILL.md ({skill.skill_md_path}) ---")
    print(skill.skill_md_path.read_text(encoding="utf-8"), end="")
    source_map_path = skill.skill_md_path.parent / "references" / "source-map.md"
    if source_map_path.exists():
        print("\n--- source-map.md ---")
        print(source_map_path.read_text(encoding="utf-8"), end="")
    return 0


def _run_verify() -> int:
    skills = load_all_skills()
    stale = [skill for skill in skills if skill.is_stale]
    if stale:
        print(f"{len(stale)} stale skills:", file=sys.stderr)
        for skill in stale:
            print(f"  {skill.name}:", file=sys.stderr)
            for reason in skill.stale_reasons:
                print(f"    - {reason}", file=sys.stderr)
        return 1
    print(f"all {len(skills)} skills verified fresh")
    return 0
