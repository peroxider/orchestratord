"""Step 2: IssueRecord 新字段 + 持久化 back-compat.

Covers:
  - IssueRecord 默认值 (has_conflict=False, conflict_files=(), ...)
  - IssueRegistry.mark_conflict / clear_conflict / increment_rebase_attempt
  - register() 在 re-register 时保留 rebase 字段
  - 旧版 registry.json (without rebase fields) 仍能 load（默认值生效）
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestratord.issue_registry import IssueRegistry, IssueStatus


class TestIssueRecordDefaults(unittest.TestCase):
    def test_fields_have_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="1", issue_identifier="ISSUE-1")
            record = reg.get("1")
            assert record is not None
            self.assertFalse(record.has_conflict)
            self.assertEqual(tuple(record.conflict_files), ())
            self.assertEqual(record.rebase_attempt_count, 0)
            self.assertIsNone(record.last_rebase_attempt_at)


class TestMarkClearConflict(unittest.TestCase):
    def test_mark_conflict_sets_files_and_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            record = reg.mark_conflict("7", ("src/a.py", "src/b.py"))
            assert record is not None
            self.assertTrue(record.has_conflict)
            self.assertEqual(tuple(record.conflict_files), ("src/a.py", "src/b.py"))
            # Persisted to disk.
            reloaded = IssueRegistry(Path(tmp) / "r.json").get("7")
            assert reloaded is not None
            self.assertTrue(reloaded.has_conflict)
            self.assertEqual(tuple(reloaded.conflict_files), ("src/a.py", "src/b.py"))

    def test_mark_conflict_empty_clears_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            reg.mark_conflict("7", ("src/a.py",))
            reg.mark_conflict("7", ())
            record = reg.get("7")
            assert record is not None
            self.assertTrue(record.has_conflict)
            self.assertEqual(tuple(record.conflict_files), ())

    def test_clear_conflict_resets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            reg.mark_conflict("7", ("src/a.py",))
            reg.clear_conflict("7")
            record = reg.get("7")
            assert record is not None
            self.assertFalse(record.has_conflict)
            self.assertEqual(tuple(record.conflict_files), ())

    def test_clear_conflict_unknown_id_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            self.assertIsNone(reg.clear_conflict("missing"))

    def test_mark_conflict_unknown_id_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            self.assertIsNone(reg.mark_conflict("missing", ("x.py",)))


class TestIncrementRebaseAttempt(unittest.TestCase):
    def test_increment_from_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            record = reg.increment_rebase_attempt("7")
            assert record is not None
            self.assertEqual(record.rebase_attempt_count, 1)
            self.assertIsNotNone(record.last_rebase_attempt_at)

    def test_increment_multiple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            for expected in (1, 2, 3, 4):
                record = reg.increment_rebase_attempt("7")
                assert record is not None
                self.assertEqual(record.rebase_attempt_count, expected)

    def test_increment_unknown_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            self.assertIsNone(reg.increment_rebase_attempt("missing"))


class TestReregisterPreservesFields(unittest.TestCase):
    def test_reregister_keeps_conflict_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            reg.mark_conflict("7", ("src/a.py",))
            # Re-register with new metadata — rebase fields preserved.
            reg.register(issue_id="7", issue_identifier="ISSUE-7-renamed")
            record = reg.get("7")
            assert record is not None
            self.assertTrue(record.has_conflict)
            self.assertEqual(tuple(record.conflict_files), ("src/a.py",))

    def test_reregister_keeps_rebase_attempt_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            reg.increment_rebase_attempt("7")
            reg.increment_rebase_attempt("7")
            reg.register(issue_id="7", issue_identifier="ISSUE-7")
            record = reg.get("7")
            assert record is not None
            self.assertEqual(record.rebase_attempt_count, 2)


class TestBackwardCompatPreRegistry(unittest.TestCase):
    def test_legacy_registry_loads_with_defaults(self) -> None:
        """A registry.json written before this feature existed has no rebase fields.
        After load, the rebase fields take their defaults so callers
        can still read them without KeyError."""
        with tempfile.TemporaryDirectory() as tmp:
            reg_path = Path(tmp) / "r.json"
            legacy_record = {
                "issue_id": "1",
                "issue_identifier": "ISSUE-1",
                "branch_name": "feature/old",
                "status": "completed",
                "attempt_count": 1,
                "retry_count": 0,
            }
            reg_path.write_text(
                json.dumps({"1": legacy_record}),
                encoding="utf-8",
            )
            reg = IssueRegistry(reg_path)
            record = reg.get("1")
            assert record is not None
            self.assertFalse(record.has_conflict)
            self.assertEqual(tuple(record.conflict_files), ())
            self.assertEqual(record.rebase_attempt_count, 0)
            self.assertIsNone(record.last_rebase_attempt_at)

    def test_partial_legacy_record_loads(self) -> None:
        """Even a registry that mixes old + new field naming should
        not crash on load — defaults fill the gaps."""
        with tempfile.TemporaryDirectory() as tmp:
            reg_path = Path(tmp) / "r.json"
            reg_path.write_text(
                json.dumps(
                    {
                        "1": {
                            "issue_id": "1",
                            "issue_identifier": "ISSUE-1",
                            "branch_name": None,
                            "base_branch": "main",
                            "status": "pending",
                            "has_conflict": True,
                            "conflict_files": ["src/x.py"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            reg = IssueRegistry(reg_path)
            record = reg.get("1")
            assert record is not None
            self.assertTrue(record.has_conflict)
            self.assertEqual(list(record.conflict_files), ["src/x.py"])
            # Missing fields default.
            self.assertEqual(record.rebase_attempt_count, 0)


if __name__ == "__main__":
    unittest.main()


class TestRunDiagnosticsCost(unittest.TestCase):
    """Run diagnostics must carry cost telemetry."""

    def test_update_run_diagnostics_persists_cost_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="9", issue_identifier="ISSUE-9")

            record = reg.update_run_diagnostics(
                "9",
                run_id="run-9",
                cost_usd=0.1234,
                token_usage={"inputTokens": 4287, "outputTokens": 34},
            )
            assert record is not None
            self.assertEqual(record.run_cost_usd, 0.1234)
            self.assertEqual(
                record.run_token_usage, {"inputTokens": 4287, "outputTokens": 34}
            )
            # Persisted to disk.
            reloaded = IssueRegistry(Path(tmp) / "r.json").get("9")
            assert reloaded is not None
            self.assertEqual(reloaded.run_cost_usd, 0.1234)
            self.assertEqual(
                reloaded.run_token_usage, {"inputTokens": 4287, "outputTokens": 34}
            )


def test_tracker_config_has_no_dead_cordis_key() -> None:
    """Tracker.cordis documented "forwarded via DSH_CORDIS_CONFIG"
    but no code ever read it — the real chain is agent.cordis →
    SessionSpec.cordis. The dead key must be gone.
    """
    from orchestratord.config.schema import TrackerConfig

    assert "cordis" not in TrackerConfig.__dataclass_fields__
