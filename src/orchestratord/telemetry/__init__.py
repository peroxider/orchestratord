"""Orchestratord telemetry.

Run-level telemetry that orchestratord owns itself — decoupled from the
clawcodex ``telemetry`` package:

- ``storage``: append-only JSONL events under ``~/.orchestratord/telemetry/events/``
- ``recorder``: record_* helpers (session / command / error / turn / usage)

Reporting (daily aggregation to a remote issue) is phase 2 and will live
under ``reporters/``.
"""

from .recorder import (
    get_recorder,
    record_command_run,
    record_crash,
    record_error,
    record_session_end,
    record_session_start,
    record_tool_summary,
    record_turn,
    record_usage,
    record_verification,
)
from .storage import events_dir, read_events, telemetry_dir

__all__ = [
    "get_recorder",
    "record_command_run",
    "record_crash",
    "record_error",
    "record_session_end",
    "record_session_start",
    "record_tool_summary",
    "record_turn",
    "record_usage",
    "record_verification",
    "events_dir",
    "read_events",
    "telemetry_dir",
]
