"""Event log → DB migration safety (Phase 0, §10.2).

Pins the contract for ``scripts/migrate_eventlog_to_db.py``. The script
consumes legacy disk JSONL event logs (pre-PostgreSQL storage) and bulk-
loads them into the new ``events`` table. Safety invariants:

* Idempotent (offset tracker stored per file; re-run picks up at last byte).
* Tolerant of partial trailing lines (a process crash mid-write).
* Dry-run mode exists for pre-flight safety checks.
* No FK violations: events whose ``workspace_id`` is unknown are
  quarantined (not silently dropped).

Reference: docs/FEATURE_GAP_VS_MULTICA.md §10.2.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers: stand-ins for the production targets. Tests use sqlite3 in-memory
# so they don't depend on PostgreSQL being available.
# ---------------------------------------------------------------------------

def _make_fake_db() -> sqlite3.Connection:
    """Build an in-memory sqlite that mimics the events table schema.

    Schema mirrors ``events`` from §6.1.1 (simplified):
        id INTEGER PK, workspace_id TEXT, session_id TEXT, sequence INT,
        kind TEXT, payload TEXT (JSON), created_at TIMESTAMP
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT,
            session_id TEXT,
            sequence INTEGER,
            kind TEXT,
            payload TEXT,
            created_at TIMESTAMP
        );
        CREATE TABLE migration_state (
            source_path TEXT PRIMARY KEY,
            last_offset INTEGER NOT NULL DEFAULT 0,
            migrated_at TIMESTAMP
        );
        """
    )
    return conn


def _write_jsonl(path: Path, lines: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicMigration:
    """The happy path: a clean JSONL file migrates in order."""

    def test_one_event_per_jsonl_line(self, tmp_path: Path) -> None:
        from scripts.migrate_eventlog_to_db import migrate_file  # type: ignore

        log = tmp_path / "events.jsonl"
        events = [
            {
                "workspace_id": "ws_a", "session_id": "s1", "sequence": 1,
                "kind": "TEXT_DELTA", "payload": {"text": "hello"},
                "created_at": "2026-09-05T10:00:00Z",
            },
            {
                "workspace_id": "ws_a", "session_id": "s1", "sequence": 2,
                "kind": "TURN_COMPLETE", "payload": {},
                "created_at": "2026-09-05T10:00:01Z",
            },
        ]
        _write_jsonl(log, events)

        db = _make_fake_db()
        report = migrate_file(log, db)

        assert report["inserted"] == 2
        assert report["skipped"] == 0
        rows = db.execute(
            "SELECT kind FROM events ORDER BY sequence"
        ).fetchall()
        assert [r[0] for r in rows] == ["TEXT_DELTA", "TURN_COMPLETE"]


class TestSkippedCount:
    """Invalid complete JSONL lines are skipped and counted, not fatal."""

    def test_invalid_line_is_skipped_and_counted(self, tmp_path: Path) -> None:
        from scripts.migrate_eventlog_to_db import migrate_file  # type: ignore

        log = tmp_path / "events.jsonl"
        log.write_text(
            '{"workspace_id":"w","session_id":"s","sequence":1,'
            '"kind":"TEXT","payload":{},"created_at":"2026-09-05T10:00:00Z"}\n'
            'this is not valid json\n'
            '{"workspace_id":"w","session_id":"s","sequence":2,'
            '"kind":"TEXT","payload":{},"created_at":"2026-09-05T10:00:01Z"}\n',
            encoding="utf-8",
        )
        db = _make_fake_db()
        report = migrate_file(log, db)

        assert report["inserted"] == 2
        assert report["skipped"] == 1


class TestIdempotency:
    """Re-running the migration must not double-insert."""

    def test_second_run_is_a_noop(self, tmp_path: Path) -> None:
        from scripts.migrate_eventlog_to_db import migrate_file  # type: ignore

        log = tmp_path / "events.jsonl"
        _write_jsonl(log, [
            {"workspace_id": "w", "session_id": "s", "sequence": 1,
             "kind": "TEXT", "payload": {}, "created_at": "2026-09-05T10:00:00Z"},
        ])
        db = _make_fake_db()

        migrate_file(log, db)
        first_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        migrate_file(log, db)
        second_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        assert first_count == 1
        assert second_count == 1, "migration was not idempotent"


class TestPartialTrailingLines:
    """Crash mid-write leaves last line truncated; must not crash."""

    def test_truncated_trailing_line_is_skipped(self, tmp_path: Path) -> None:
        from scripts.migrate_eventlog_to_db import migrate_file  # type: ignore

        log = tmp_path / "events.jsonl"
        log.write_text(
            '{"workspace_id":"w","session_id":"s","sequence":1,'
            '"kind":"TEXT","payload":{},"created_at":"2026-09-05T10:00:00Z"}\n'
            '{"workspace_id":"w","session_id":"s","sequence":2,"kin',
            encoding="utf-8",
        )
        db = _make_fake_db()
        report = migrate_file(log, db)

        assert report["inserted"] == 1
        assert report["truncated_tail_dropped"] == 1


class TestDryRun:
    """Dry-run mode must never insert or mutate the offset tracker."""

    def test_dry_run_reports_would_insert(self, tmp_path: Path) -> None:
        from scripts.migrate_eventlog_to_db import migrate_file  # type: ignore

        log = tmp_path / "events.jsonl"
        _write_jsonl(log, [
            {"workspace_id": "w", "session_id": "s", "sequence": 1,
             "kind": "TEXT", "payload": {}, "created_at": "2026-09-05T10:00:00Z"},
        ])
        db = _make_fake_db()

        report = migrate_file(log, db, dry_run=True)

        assert report["would_insert"] == 1
        assert report["inserted"] == 0
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        state = db.execute(
            "SELECT last_offset FROM migration_state WHERE source_path = ?",
            (str(log),),
        ).fetchone()
        assert state is None


class TestOrphanQuarantine:
    """Events referencing an unknown workspace must NOT silently drop."""

    def test_event_with_missing_workspace_is_quarantined(
        self, tmp_path: Path,
    ) -> None:
        from scripts.migrate_eventlog_to_db import (  # type: ignore
            migrate_file_with_registry,
        )

        log = tmp_path / "events.jsonl"
        _write_jsonl(log, [
            {"workspace_id": "ws_unknown", "session_id": "s", "sequence": 1,
             "kind": "TEXT", "payload": {}, "created_at": "2026-09-05T10:00:00Z"},
            {"workspace_id": "ws_known", "session_id": "s", "sequence": 2,
             "kind": "TEXT", "payload": {}, "created_at": "2026-09-05T10:00:01Z"},
        ])

        conn = _make_fake_db()
        # sqlite has no `CREATE TABLE ... (LIKE ... INCLUDING ALL)`; mirror
        # the events schema explicitly so the quarantine target exists.
        conn.execute(
            """
            CREATE TABLE quarantined_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id TEXT,
                session_id TEXT,
                sequence INTEGER,
                kind TEXT,
                payload TEXT,
                created_at TIMESTAMP
            )
            """
        )

        report = migrate_file_with_registry(
            log,
            conn,
            known_workspace_ids={"ws_known"},
        )
        assert report["inserted"] == 1
        assert report["quarantined"] == 1
        qcount = conn.execute(
            "SELECT COUNT(*) FROM quarantined_events"
        ).fetchone()[0]
        assert qcount == 1
