"""orchestratord rules — learn coding conventions from PR review feedback.

Usage (noun-verb):
  orchestratord rules learn [--workspace PATH] [--repo PATH] [--limit N]
  orchestratord rules show [--workspace PATH]

Design:
  - Scans the project repository for review follow-up commits (commits
    whose message carries ``review-pr:`` / ``review-body:`` trailers
    written after a review round).
  - For each unprocessed follow-up commit, an LLM extracts ONE coding
    convention from the review + diff. Candidates are deduplicated
    against existing rules with
    :class:`~orchestratord.rules_learner.BatchedLLMJudge`.
  - Learned rules are persisted to the JSON file configured under the
    ``rules.path`` workflow config key (capped at ``rules.max_rules``);
    processed commit SHAs are recorded alongside so each follow-up is
    analyzed exactly once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..rules_learner import BatchedLLMJudge


# ── Rules store helpers ──────────────────────────────────────────────


def _rules_file_for(workspace_root: Path, configured_path: str) -> Path:
    """Resolve the rules JSON file path from config (or a default)."""
    if configured_path:
        return Path(configured_path).expanduser()
    return workspace_root / "rules.json"


def _load_rules(rules_path: Path) -> list[dict[str, Any]]:
    """Load the existing rules list (empty when missing/corrupt)."""
    try:
        if not rules_path.exists():
            return []
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"    ⚠ Failed to load rules file {rules_path}: {exc}")
        return []
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    return []


def _save_rules(rules_path: Path, rules: list[dict[str, Any]]) -> None:
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_processed(processed_path: Path) -> set[str]:
    """Load the set of already-analyzed commit SHAs."""
    try:
        if not processed_path.exists():
            return set()
        data = json.loads(processed_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(data, list):
        return {str(item) for item in data}
    return set()


def _save_processed(processed_path: Path, processed: set[str]) -> None:
    try:
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        processed_path.write_text(
            json.dumps(sorted(processed), indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"    ⚠ Failed to persist processed set: {exc}")


def _load_rules_config(workspace_root: Path) -> dict[str, Any]:
    """Read the ``rules:`` section from ``workflow.yaml`` (best effort)."""
    config_path = workspace_root / "workflow.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rules = (raw or {}).get("rules") if isinstance(raw, dict) else None
    return rules if isinstance(rules, dict) else {}


# ── Git helpers ──────────────────────────────────────────────────────


def _git(repo: Path, args: list[str]) -> str:
    """Run a git command in *repo*, returning stdout ("" on failure)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"    ⚠ git {' '.join(args[:2])} failed: {exc}")
        return ""


def _find_repo(workspace_root: Path) -> Path | None:
    """Walk up from *workspace_root* to find a git repository root."""
    current = workspace_root
    for _ in range(6):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ── Parser ───────────────────────────────────────────────────────────


def add_rules_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``rules`` sub-subcommands (learn | show)."""
    parser = subparsers.add_parser(
        "rules",
        help="Learn and inspect coding-convention rules",
        description="Extract coding conventions from PR review follow-up "
        "commits, deduplicate them, and persist them to the rules file.",
    )
    rules_sub = parser.add_subparsers(
        dest="rules_subcommand",
        required=True,
    )

    # --- rules learn ---
    learn_parser = rules_sub.add_parser(
        "learn",
        help="Learn rules from new review follow-up commits",
        description="Scan the repository for unprocessed review follow-up "
        "commits and extract one coding convention per commit.",
    )
    learn_parser.add_argument(
        "--workspace",
        default=".",
        metavar="PATH",
        help="Workspace root holding workflow.yaml (default: .)",
    )
    learn_parser.add_argument(
        "--repo",
        default="",
        metavar="PATH",
        help="Git repository to scan (default: nearest repo above --workspace)",
    )
    learn_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Maximum number of follow-up commits to scan (default: 50)",
    )

    # --- rules show ---
    show_parser = rules_sub.add_parser(
        "show",
        help="Print the currently learned rules",
        description="Pretty-print the rules file for the given workspace.",
    )
    show_parser.add_argument(
        "--workspace",
        default=".",
        metavar="PATH",
        help="Workspace root holding workflow.yaml (default: .)",
    )


# ── Dispatch ─────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> int:
    """Dispatch to the appropriate rules subcommand."""
    cmd = args.rules_subcommand
    if cmd == "learn":
        return asyncio.run(_run_learn(args))
    elif cmd == "show":
        return _run_show(args)
    print(f"error: unknown rules subcommand '{cmd}'", file=sys.stderr)
    return 2


# ── Implementations ──────────────────────────────────────────────────


def _run_show(args: argparse.Namespace) -> int:
    """Print the learned rules file."""
    workspace_root = Path(args.workspace).expanduser().resolve()
    rules_cfg = _load_rules_config(workspace_root)
    rules_path = _rules_file_for(workspace_root, str(rules_cfg.get("path", "")))
    rules = _load_rules(rules_path)
    if not rules:
        print(f"No rules learned yet (expected at {rules_path}).")
        return 0
    print(f"Rules ({len(rules)}) — {rules_path}:")
    print()
    for index, rule in enumerate(rules, start=1):
        print(f"{index}. [{rule.get('category', 'other')}] {rule.get('summary', '')}")
        body = str(rule.get("body", "") or "").strip()
        if body:
            print(f"   {body}")
    return 0


async def _run_learn(args: argparse.Namespace) -> int:
    """Scan review follow-up commits and learn coding conventions.

    Per commit:
      # 1. Resolve the workspace and the ``rules:`` config section.
      # 2. Load existing rules and the processed-commit set.
      # 3. Locate the git repository to scan.
      # 4. Collect candidate follow-up commits (messages with
         ``review-pr:`` trailers) not yet processed.
      # 5. For each candidate, read the diff and review trailers.
      # 6. Build the LLM extraction prompt (see below).
      # 7. Parse the extracted rule and deduplicate it against the
         existing rules via the batched judge.
    """
    # 1. Workspace + rules config
    workspace_root = Path(args.workspace).expanduser().resolve()
    if not workspace_root.exists():
        print(f"✗ Workspace not found: {workspace_root}", file=sys.stderr)
        return 1
    rules_cfg = _load_rules_config(workspace_root)
    if rules_cfg and not bool(rules_cfg.get("enabled", False)):
        print("Rules learning is disabled (rules.enabled=false) — nothing to do.")
        return 0

    # 2. Rules file + processed set
    rules_path = _rules_file_for(workspace_root, str(rules_cfg.get("path", "")))
    rules = _load_rules(rules_path)
    max_rules = int(rules_cfg.get("max_rules", 20) or 20)
    processed_path = rules_path.parent / f".{rules_path.stem}_processed.json"
    processed = _load_processed(processed_path)

    # 3. Repository
    repo = Path(args.repo).expanduser().resolve() if args.repo else _find_repo(workspace_root)
    if repo is None or not (repo / ".git").exists():
        print("✗ No git repository found — pass --repo explicitly.", file=sys.stderr)
        return 1

    # 4. Candidate follow-up commits
    log = _git(
        repo,
        ["log", "-n", str(max(1, int(args.limit))), "--format=%H%x1f%B%x1e"],
    )
    candidates: list[tuple[str, str]] = []
    for entry in log.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        sha, _, message = entry.partition("\x1f")
        sha = sha.strip()
        if sha and sha not in processed and "review-pr:" in message:
            candidates.append((sha, message))
    if not candidates:
        print("No new review follow-up commits found — nothing to learn.")
        return 0

    print(f"Found {len(candidates)} candidate review follow-up commit(s) in {repo}")
    count = 0
    try:
        for sha, message in candidates:
            # 5. Diff + review trailers
            print(f"  • {sha[:12]}")
            diff = _git(repo, ["show", "--format=", "--unified=2", sha])
            review_pr = ""
            review_body = ""
            for line in message.splitlines():
                line = line.strip()
                if line.startswith("review-pr:"):
                    review_pr = line.split(":", 1)[1].strip()
                elif line.startswith("review-body:"):
                    review_body = line.split(":", 1)[1].strip()

            # 6. Build prompt for LLM
            judge = BatchedLLMJudge()
            judge_prompt = (
                f"Analyze this PR review follow-up commit and extract a coding "
                f"convention that should be followed going forward.\n\n"
                f"Review (PR {review_pr}): {review_body}\n\n"
                f"Code diff:\n```diff\n{diff}\n```\n\n"
                f"Extract ONE coding convention from this review. "
                f"Output it in this EXACT format:\n\n"
                f"- [category] Short summary of the convention\n"
                f"  Body: Detailed explanation with rationale. You MUST include this line.\n\n"
                f"category MUST be one of:\n"
                f'  naming          — e.g. "[naming] Use snake_case for function names"\n'
                f'  error_handling  — e.g. "[error_handling] Catch specific exceptions, not bare except"\n'
                f'  testing         — e.g. "[testing] Use pytest fixtures for shared setup"\n'
                f'  import_style    — e.g. "[import_style] Group stdlib imports first"\n'
                f'  code_style      — e.g. "[code_style] Use double quotes for string literals"\n'
                f'  type_annotation — e.g. "[type_annotation] Add return type to public functions"\n'
                f'  architecture    — e.g. "[architecture] Keep business logic out of route handlers"\n'
                f'  boilerplate     — e.g. "[boilerplate] Every module starts with a license header"\n'
                f'  security        — e.g. "[security] Never log API keys or tokens"\n'
                f'  performance     — e.g. "[performance] Use generator expressions for large datasets"\n'
                f"  other           — only if no category above fits\n\n"
                f"The Body line is MANDATORY — explain WHY this convention matters "
                f"and give a brief example."
            )

            try:
                # LLM extraction is deferred to SPI LLM provider (Phase 4).
                # For now, skip the LLM analysis and mark as processed.
                print(f"    ⚠ LLM analysis skipped (SPI LLM provider not yet wired)")
                llm_reply = ""
            except Exception as exc:
                print(f"    ⚠ LLM analysis failed: {exc}")
                processed.add(sha)
                count += 1
                continue

            if not llm_reply.strip():
                print(f"    ⚠ LLM returned empty, record as processed")
                processed.add(sha)
                count += 1
                continue

            # 7. Parse the extracted rule and deduplicate.
            rule = _parse_rule_reply(llm_reply)
            if rule is None:
                print("    ⚠ Could not parse the extracted rule, record as processed")
                processed.add(sha)
                count += 1
                continue

            verdicts = await judge.judge([rule], rules)
            action = str(getattr(verdicts[0], "action", "new")) if verdicts else "new"
            if action == "duplicate":
                print("    = Duplicate of an existing rule — skipped")
            elif len(rules) >= max_rules:
                print(f"    ! Rules file is full (max_rules={max_rules}) — stopping")
                break
            else:
                rules.append(rule)
                _save_rules(rules_path, rules)
                print(f"    ✓ Learned [{rule.get('category')}] {rule.get('summary')}")
            processed.add(sha)
            count += 1
    except KeyboardInterrupt:
        print("\nInterrupted — progress saved.")

    _save_processed(processed_path, processed)
    print()
    print(f"Done: {count} follow-up commit(s) processed, {len(rules)} rule(s) on file.")
    return 0


def _parse_rule_reply(reply: str) -> dict[str, str] | None:
    """Parse the ``- [category] summary`` / ``Body: ...`` reply format."""
    category = ""
    summary = ""
    body = ""
    for line in reply.splitlines():
        stripped = line.strip()
        if not category and stripped.startswith("- [") and "]" in stripped:
            end = stripped.index("]")
            category = stripped[3:end].strip().lower() or "other"
            summary = stripped[end + 1 :].strip()
        elif stripped.lower().startswith("body:"):
            body = stripped.split(":", 1)[1].strip()
        elif body and stripped:
            body += " " + stripped
    if not summary or not body:
        return None
    return {
        "category": category or "other",
        "summary": summary,
        "body": body,
    }
