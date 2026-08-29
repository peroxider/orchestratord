"""Unified path management for orchestratord.

All ~/.orchestratord/ paths are centralized here.

Import conventions:
    from orchestratord.paths import (
        ORCHESTRATORD_BASE,
        SESSIONS_DIR,
        TOOL_EVENTS_DIR,
        ...
    )
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Base directories
# ---------------------------------------------------------------------------

ORCHESTRATORD_BASE = Path.home() / ".orchestratord"

# ---------------------------------------------------------------------------
# Subdirectories
# ---------------------------------------------------------------------------

ORCHESTRATOR_DIR = ORCHESTRATORD_BASE / "orchestrator"
SESSIONS_DIR = ORCHESTRATORD_BASE / "sessions"
TOOL_EVENTS_DIR = ORCHESTRATORD_BASE / "tool-events"
WORKFLOW_EVENTS_DIR = ORCHESTRATORD_BASE / "workflow-events"
GATEWAY_DIR = ORCHESTRATORD_BASE / "gateway"


# ---------------------------------------------------------------------------
# Dotfile markers (workspace-level)
# ---------------------------------------------------------------------------

DOTFILES = {
    "issue_registry": ".orchestratord_issue_registry.json",
    "clarification_queue": ".orchestratord_clarification_queue.json",
    "clarifier_cache": ".orchestratord_issue_clarifier_cache.json",
    "workspace_lock": ".orchestratord_workspace.lock",
    "team_config": ".orchestratord_team.json",
}

# ---------------------------------------------------------------------------
# Specific file paths
# ---------------------------------------------------------------------------

# Audit log
AUDIT_LOG = ORCHESTRATOR_DIR / "audit.jsonl"

# Gateway socket
GATEWAY_SOCK = GATEWAY_DIR / "gateway.sock"

# Clarification queue
CLARIFICATION_QUEUE_FILE = ORCHESTRATORD_BASE / "clarification_queue.json"


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------

def resolve_audit_log() -> Path:
    """Return the audit log directory, creating it if needed."""
    ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIT_LOG


def resolve_sessions_dir() -> Path:
    """Return the sessions directory, creating it if needed."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def resolve_tool_events_dir() -> Path:
    """Return the tool-events directory, creating it if needed."""
    TOOL_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    return TOOL_EVENTS_DIR


def resolve_workflow_events_dir() -> Path:
    """Return the workflow-events directory, creating it if needed."""
    WORKFLOW_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    return WORKFLOW_EVENTS_DIR


def resolve_gateway_dir() -> Path:
    """Return the gateway directory, creating it if needed."""
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    return GATEWAY_DIR


def resolve_gateway_sock() -> Path:
    """Return the gateway socket path."""
    return resolve_gateway_dir() / "gateway.sock"


def resolve_clarification_queue_file() -> Path:
    """Return the clarification queue file path."""
    ORCHESTRATORD_BASE.mkdir(parents=True, exist_ok=True)
    return CLARIFICATION_QUEUE_FILE


def resolve_file_with_fallback(
    new_path: Path,
    legacy_path: Path,
    *,
    for_write: bool = False,
) -> Path:
    """Resolve a file path with backward-compat fallback for reads.

    For writes: always return the new path (creating parent dirs).
    For reads: return new_path if it exists, otherwise legacy_path
    (even if legacy_path doesn't exist — caller decides what to do).

    Args:
        new_path: The canonical new path.
        legacy_path: The legacy path to fall back to for reads.
        for_write: If True, always return new_path and create parent dirs.

    Returns:
        The resolved path.
    """
    if for_write:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        return new_path
    if new_path.exists():
        return new_path
    return legacy_path
