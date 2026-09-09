"""Orchestratord telemetry — run-level event storage (append-only JSONL).

Events are written under ``~/.orchestratord/telemetry/events/<YYYY-MM-DD>.jsonl``
(``ORCHESTRATORD_HOME`` overrides the base dir). One JSON object per line.
This module is self-contained (stdlib only) — it does NOT depend on the
clawcodex ``telemetry`` package, so orchestratord telemetry works even when
clawcodex telemetry is disabled.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_TELEMETRY_DIR = Path(
    os.environ.get("ORCHESTRATORD_HOME", str(Path.home() / ".orchestratord"))
) / "telemetry"
_EVENTS_DIR = _TELEMETRY_DIR / "events"


def telemetry_dir() -> Path:
    _TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    return _TELEMETRY_DIR


def events_dir() -> Path:
    _EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    return _EVENTS_DIR


def append_event(event: dict) -> None:
    """Append one event object as a JSON line to today's events file."""
    day = time.strftime("%Y-%m-%d")
    path = events_dir() / f"{day}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(day: str | None = None) -> list[dict]:
    """Read back the events for one day (default: today) for aggregation/reporting."""
    day = day or time.strftime("%Y-%m-%d")
    path = events_dir() / f"{day}.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def local_days() -> list[str]:
    """List the days that have a local events file, oldest first.

    Day keys come from the ``<YYYY-MM-DD>.jsonl`` filenames; anything
    that does not parse as a date (stray files) is ignored.
    """
    days: list[str] = []
    for path in _EVENTS_DIR.glob("*.jsonl"):
        name = path.stem
        try:
            time.strptime(name, "%Y-%m-%d")
        except ValueError:
            continue
        days.append(name)
    return sorted(days)
