"""Auto-generation of verify.sh and README.md for preserved workspaces.

When a workspace is preserved after an issue completes (or fails), these
functions generate helper files to make manual verification easier:

- verify.sh: One-click script to re-run test/build/lint commands
- README.md: Documentation about the workspace, issue, and changes
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def generate_verify_script(
    workspace_path: Path,
    agent_config: Any,
    issue_record: Any,
) -> None:
    """Generate verify.sh script for manual verification.

    Args:
        workspace_path: Path to the preserved workspace
        agent_config: AgentConfig with test/build/lint commands
        issue_record: IssueRecord with issue metadata
    """
    test_cmd = getattr(agent_config, "test_command", None)
    build_cmd = getattr(agent_config, "build_command", None)
    lint_cmd = getattr(agent_config, "lint_command", None)

    # Skip if no verification commands configured
    if not any([test_cmd, build_cmd, lint_cmd]):
        logger.debug("No verification commands configured, skipping verify.sh generation")
        return

    sub_dir = workspace_path / ".orchestrator_workspace"
    sub_dir.mkdir(exist_ok=True)
    verify_path = sub_dir / "verify.sh"

    lines = [
        "#!/usr/bin/env bash",
        "# Orchestratord Verify Script",
        "#",
        "# This script re-runs the verification commands that were executed",
        "# during the automated issue processing.",
        "#",
        f"# Issue: {getattr(issue_record, 'issue_id', 'unknown')}",
        f"# Branch: {getattr(issue_record, 'branch_name', 'unknown')}",
        f"# Commit: {getattr(issue_record, 'commit_sha', 'unknown')}",
        f"# Status: {getattr(issue_record, 'status', 'unknown')}",
        "#",
        "# Usage: ./verify.sh",
        "#",
        "set -e",
        "",
        "echo '=== Orchestratord Verification ==='",
        f"echo 'Issue: {getattr(issue_record, 'issue_id', 'unknown')}'",
        f"echo 'Branch: {getattr(issue_record, 'branch_name', 'unknown')}'",
        f"echo 'Commit: {getattr(issue_record, 'commit_sha', 'unknown')}'",
        "echo ''",
        "",
    ]

    if build_cmd:
        lines.extend(
            [
                "echo '>>> Running build...'",
                f"{build_cmd}",
                "echo 'Build: OK'",
                "echo ''",
                "",
            ]
        )

    if lint_cmd:
        lines.extend(
            [
                "echo '>>> Running lint...'",
                f"{lint_cmd}",
                "echo 'Lint: OK'",
                "echo ''",
                "",
            ]
        )

    if test_cmd:
        lines.extend(
            [
                "echo '>>> Running tests...'",
                f"{test_cmd}",
                "echo 'Tests: OK'",
                "echo ''",
                "",
            ]
        )

    lines.extend(
        [
            "echo '=== All verification steps passed ==='",
        ]
    )

    try:
        verify_path.write_text("\n".join(lines), encoding="utf-8")
        verify_path.chmod(0o755)
        logger.info("Generated verify.sh at %s", verify_path)
    except Exception as e:
        logger.warning("Failed to generate verify.sh: %s", e)


def generate_workspace_readme(
    workspace_path: Path,
    issue_record: Any,
    git_sync_result: Any = None,
) -> None:
    """Generate README.md documenting the preserved workspace.

    Args:
        workspace_path: Path to the preserved workspace
        issue_record: IssueRecord with issue metadata
        git_sync_result: Optional GitSyncResult with commit/PR info
    """
    sub_dir = workspace_path / ".orchestrator_workspace"
    sub_dir.mkdir(exist_ok=True)
    readme_path = sub_dir / "README.md"

    issue_id = getattr(issue_record, "issue_id", "unknown")
    identifier = getattr(issue_record, "identifier", "unknown")
    status = getattr(issue_record, "status", "unknown")
    branch = getattr(issue_record, "branch_name", "unknown")
    commit = getattr(issue_record, "commit_sha", "unknown")
    pr_url = getattr(issue_record, "pr_url", None)
    verification = getattr(issue_record, "verification_status", None)

    lines = [
        f"# Workspace: {identifier}",
        "",
        "This workspace was preserved by orchestratord after automated issue processing.",
        "",
        "## Issue Information",
        "",
        f"- **Issue ID**: {issue_id}",
        f"- **Identifier**: {identifier}",
        f"- **Status**: {status}",
        f"- **Branch**: `{branch}`",
        f"- **Commit**: `{commit}`",
    ]

    if pr_url:
        lines.append(f"- **Pull Request**: {pr_url}")

    if verification:
        lines.append(f"- **Verification**: {verification}")

    lines.extend(
        [
            "",
            "## Workspace Contents",
            "",
            "This directory contains the code changes made by the selected agent backend for this issue.",
            "",
        ]
    )

    # List top-level files/directories
    try:
        items = sorted(workspace_path.iterdir())
        if items:
            lines.append("### Files and Directories")
            lines.append("")
            for item in items[:20]:  # Limit to first 20 items
                if item.name.startswith("."):
                    continue
                prefix = "📁" if item.is_dir() else "📄"
                lines.append(f"- {prefix} `{item.name}`")
            if len(items) > 20:
                lines.append(f"- ... and {len(items) - 20} more")
            lines.append("")
    except Exception:
        pass

    lines.extend(
        [
            "## Verification",
            "",
            "To verify the changes manually, run:",
            "",
            "```bash",
            "./.orchestrator_workspace/verify.sh",
            "```",
            "",
            "Or run individual commands:",
            "",
        ]
    )

    # Add manual verification instructions
    test_cmd = "pytest"  # default
    build_cmd = None
    lint_cmd = None

    # Try to detect commands from common files
    if (workspace_path / "package.json").exists():
        test_cmd = "npm test"
        build_cmd = "npm run build"
    elif (workspace_path / "Cargo.toml").exists():
        test_cmd = "cargo test"
        build_cmd = "cargo build"
    elif (workspace_path / "go.mod").exists():
        test_cmd = "go test ./..."
        build_cmd = "go build ./..."
    elif (workspace_path / "pyproject.toml").exists() or (workspace_path / "setup.py").exists():
        test_cmd = "pytest"

    if build_cmd:
        lines.append(f"```bash")
        lines.append(f"# Build")
        lines.append(f"{build_cmd}")
        lines.append(f"```")
        lines.append("")

    if lint_cmd:
        lines.append(f"```bash")
        lines.append(f"# Lint")
        lines.append(f"{lint_cmd}")
        lines.append(f"```")
        lines.append("")

    lines.extend(
        [
            "```bash",
            "# Test",
            f"{test_cmd}",
            "```",
            "",
            "## Cleanup",
            "",
            "To remove this preserved workspace:",
            "",
            "```bash",
            f"orchestratord workspace cleanup --id {issue_id} --force",
            "```",
            "",
            "---",
            "",
            "*Generated by [orchestratord](https://github.com/your-org/orchestratord)*",
        ]
    )

    try:
        readme_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Generated README.md at %s", readme_path)
    except Exception as e:
        logger.warning("Failed to generate README.md: %s", e)
