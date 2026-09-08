"""Event log → DB migration (§10.2).

Consumes legacy disk JSONL event logs (pre-PostgreSQL storage) and bulk-loads
them into the new ``events`` table. Safety invariants:

* Idempotent — a per-file byte offset is stored in ``migration_state`` so a
  re-run picks up at the last consumed byte.
* Tolerant of partial trailing lines — a crash mid-write leaves the final
  line truncated; it is dropped and reported, not fatal.
* Dry-run mode exists for pre-flight safety checks (no insert, no offset
  advance).
* Orphan events — events whose ``workspace_id`` is unknown are quarantined
  into ``quarantined_events`` (not silently dropped) when the registry-aware
  entry point is used.

The tests exercise this against an in-memory sqlite3 that mimics the
``events`` schema; production targets PostgreSQL (§6.1.1), but the row
shape and offset-tracking logic are identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_jsonl_events(
    path: Path, start_offset: int
) -> tuple[list[dict[str, Any]], int, bool, int]:
    """Parse complete JSONL lines starting at *start_offset*.

    Returns ``(events, next_offset, truncated_tail, skipped)``. ``next_offset``
    is the byte offset just past the last complete line (including its
    newline); a trailing fragment without a terminating newline is treated as
    a partial write and is NOT consumed, so a re-run after the writer flushes
    can pick it up. Invalid complete lines are skipped and counted in
    ``skipped``.
    """
    raw = path.read_bytes()
    remaining = raw[start_offset:]
    if not remaining:
        return [], start_offset, False, 0

    chunks = remaining.split(b"\n")
    if remaining.endswith(b"\n"):
        complete = chunks[:-1]  # final chunk is the empty string
        tail = b""
    else:
        complete = chunks[:-1]
        tail = chunks[-1]

    events: list[dict[str, Any]] = []
    skipped = 0
    next_offset = start_offset
    for chunk in complete:
        next_offset += len(chunk) + 1  # +1 for the consumed "\n"
        line = chunk.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            skipped += 1

    truncated_tail = bool(tail.strip())
    return events, next_offset, truncated_tail, skipped


def _insert_event(db: Any, table: str, event: dict[str, Any]) -> None:
    payload = event.get("payload")
    db.execute(
        f"INSERT INTO {table} "
        "(workspace_id, session_id, sequence, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            event.get("workspace_id"),
            event.get("session_id"),
            event.get("sequence"),
            event.get("kind"),
            json.dumps(payload) if payload is not None else None,
            event.get("created_at"),
        ),
    )


def _record_offset(db: Any, source_path: str, offset: int) -> None:
    db.execute(
        "INSERT INTO migration_state (source_path, last_offset, migrated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(source_path) DO UPDATE SET "
        "last_offset = excluded.last_offset, "
        "migrated_at = excluded.migrated_at",
        (source_path, offset),
    )


def migrate_file(
    source: Path | str, db: Any, dry_run: bool = False
) -> dict[str, Any]:
    """Migrate a JSONL event log into ``db`` (idempotent).

    Returns a report with ``inserted``, ``skipped``,
    ``truncated_tail_dropped`` and (when ``dry_run``) ``would_insert``.
    """
    source_path = str(source)
    row = db.execute(
        "SELECT last_offset FROM migration_state WHERE source_path = ?",
        (source_path,),
    ).fetchone()
    start = row[0] if row else 0

    events, next_offset, truncated_tail, skipped = _load_jsonl_events(
        Path(source), start
    )

    if dry_run:
        return {
            "would_insert": len(events),
            "inserted": 0,
            "skipped": skipped,
            "truncated_tail_dropped": 1 if truncated_tail else 0,
        }

    for event in events:
        _insert_event(db, "events", event)
    _record_offset(db, source_path, next_offset)
    db.commit()

    return {
        "inserted": len(events),
        "skipped": skipped,
        "truncated_tail_dropped": 1 if truncated_tail else 0,
    }


def migrate_file_with_registry(
    source: Path | str,
    db: Any,
    known_workspace_ids: set[str],
) -> dict[str, Any]:
    """Migrate with a workspace registry, quarantining orphan events.

    Events whose ``workspace_id`` is not in *known_workspace_ids* are written
    to ``quarantined_events`` rather than dropped, so no data is silently
    lost when a workspace is missing from the registry.
    """
    source_path = str(source)
    row = db.execute(
        "SELECT last_offset FROM migration_state WHERE source_path = ?",
        (source_path,),
    ).fetchone()
    start = row[0] if row else 0

    events, next_offset, truncated_tail, skipped = _load_jsonl_events(
        Path(source), start
    )

    inserted = 0
    quarantined = 0
    for event in events:
        if event.get("workspace_id") in known_workspace_ids:
            _insert_event(db, "events", event)
            inserted += 1
        else:
            _insert_event(db, "quarantined_events", event)
            quarantined += 1

    _record_offset(db, source_path, next_offset)
    db.commit()

    return {
        "inserted": inserted,
        "quarantined": quarantined,
        "skipped": skipped,
        "truncated_tail_dropped": 1 if truncated_tail else 0,
    }
