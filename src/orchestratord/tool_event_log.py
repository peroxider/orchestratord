"""Tool-event audit log data contract.

Per-tool decision rows persisted to
``~/.orchestratord/tool-events/{run_id}/events.ndjson`` by the backend runner.

Schema (8 fields, fixed order — keeps `tail`/`grep` greppable):

    ts:               float  — time.time() at write moment
    tool:             str    — event.tool_name (Bash, Read, Edit, …)
    params:           dict   — event.params (full, not redacted — see decision #7)
    approved:         bool   — event._approved (True/False); None only if
                                ApprovalPolicy skipped (shouldn't happen in
                                orchestrator headless mode after wiring)
    deny_reason:      str|None — event._deny_reason (None on approve)
    permission_mode:  str    — session_context["permission_mode"]
                                (bypassPermissions / dontAsk / acceptEdits /
                                default / plan / auto / bubble)
    turn:             int    — session.turn_count at the moment of the call
    session_run_id:   str    — session.run_id (the directory name)

Serialised as one JSON object per line (NDJSON). Append-only, no overwrite,
no schema version field (callers must add a reader-side guard if they ever
rev the schema — see decision #5 for forward compat with report_writer).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolEventLog:
    """One row in events.ndjson."""

    tool: str
    params: dict[str, Any]
