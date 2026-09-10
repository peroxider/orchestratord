"""Orchestratord telemetry — run-level event storage (append-only JSONL).

Events are written under ``~/.orchestratord/telemetry/events/<YYYY-MM-DD>.jsonl``
(``ORCHESTRATORD_HOME`` overrides the base dir). One JSON object per line.
This module is self-contained (stdlib only) — it does NOT depend on the
clawcodex ``telemetry`` package, so orchestratord telemetry works even when
clawcodex telemetry is disabled.

The base dir is resolved lazily on every call (not at import time) so
test isolation can point ``ORCHESTRATORD_HOME`` at a tmp dir via
monkeypatch without reloading this module.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _base_dir() -> Path:
    return Path(
        os.environ.get("ORCHESTRATORD_HOME", str(Path.home() / ".orchestratord"))
    )


def telemetry_dir() -> Path:
    path = _base_dir() / "telemetry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def events_dir() -> Path:
    path = telemetry_dir() / "events"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    for path in events_dir().glob("*.jsonl"):
        name = path.stem
        try:
            time.strptime(name, "%Y-%m-%d")
        except ValueError:
            continue
        days.append(name)
    return sorted(days)
