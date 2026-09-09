"""Regression tests for the 2026-09-07 ccb smoke-test root causes.

Covers four fixes:
1. approval_policy × permission_mode disconnect (resolve_approval_policy)
2. premise-check false positive on creation-intent issues
3. stale verification fields surviving mark_completed
4. claim release on shutdown-interrupted runs (mark_pending)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orchestratord.kernel.approval import (
    AskApprovalPolicy,
    NeverApprovalPolicy,
    get_approval_policy,
    resolve_approval_policy,
)
from orchestratord.config.schema import SandboxConfig
from orchestratord.issue_registry import IssueRegistry, IssueStatus
from orchestratord.premise_check import check_issue_premise


class TestResolveApprovalPolicy(unittest.TestCase):
    """Approval-policy resolution.

    Default ``sandbox.approval_policy`` is ``"never"`` (autonomous daemon);
    explicit ask/never/dict configs are honored as before.
    """

    def test_default_never_with_bypass_permissions_auto_approves(self) -> None:
        sandbox = SandboxConfig()  # approval_policy defaults to "never"
        agent = SimpleNamespace(permission_mode="bypassPermissions")
        policy = resolve_approval_policy(sandbox, agent)
        self.assertIsInstance(policy, NeverApprovalPolicy)

    def test_default_never_with_auto_mode_auto_approves(self) -> None:
        sandbox = SandboxConfig()
        agent = SimpleNamespace(permission_mode="auto")
        self.assertIsInstance(resolve_approval_policy(sandbox, agent), NeverApprovalPolicy)

    def test_default_never_auto_approves_with_dont_ask(self) -> None:
        sandbox = SandboxConfig()  # approval_policy defaults to "never"
        agent = SimpleNamespace(permission_mode="dontAsk")
        # With default "never" the policy auto-approves regardless of
        # permission_mode; explicit ask/never still work as before.
        self.assertIsInstance(resolve_approval_policy(sandbox, agent), NeverApprovalPolicy)

    def test_default_never_auto_approves_without_agent(self) -> None:
        sandbox = SandboxConfig()
        # No agent config → default "never" still auto-approves.
        self.assertIsInstance(resolve_approval_policy(sandbox, None), NeverApprovalPolicy)

    def test_explicit_string_beats_permission_mode(self) -> None:
        sandbox = SimpleNamespace(approval_policy="ask")
        agent = SimpleNamespace(permission_mode="bypassPermissions")
        self.assertIsInstance(resolve_approval_policy(sandbox, agent), AskApprovalPolicy)

        sandbox = SimpleNamespace(approval_policy="never")
        agent = SimpleNamespace(permission_mode="dontAsk")
        self.assertIsInstance(resolve_approval_policy(sandbox, agent), NeverApprovalPolicy)

    def test_explicit_structured_dict_beats_permission_mode(self) -> None:
        sandbox = SimpleNamespace(
            approval_policy={"reject": {"sandbox_approval": False, "rules": True}}
        )
        agent = SimpleNamespace(permission_mode="bypassPermissions")
        self.assertIsInstance(resolve_approval_policy(sandbox, agent), AskApprovalPolicy)

    def test_unknown_policy_name_fails_closed_and_warns(self) -> None:
        import logging

        with self.assertLogs("orchestratord.kernel.approval", level=logging.WARNING):
            policy = get_approval_policy("bypass")  # misspelt / unknown
        self.assertIsInstance(policy, AskApprovalPolicy)


class TestPremiseCreationIntent(unittest.TestCase):
    """P0-b: a missing path the issue asks to CREATE is not a broken premise."""

    def test_creation_issue_reports_no_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = {
                "title": "Add gcd module",
                "description": (
                    "1. Create a file `gcd.py` implementing a gcd function\n"
                    "2. Create a file `test_gcd.py` with assertions\n"
                    "3. Save output to `test_output.txt`"
                ),
            }
            self.assertEqual(check_issue_premise(issue, tmp), [])

    def test_bug_issue_still_reports_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = {
                "title": "Fix crash",
                "description": "`gcd.py` crashes at line 5 with ZeroDivisionError",
            }
            self.assertEqual(check_issue_premise(issue, tmp), ["gcd.py"])

    def test_mixed_issue_keeps_only_non_creation_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issue = {
                "title": "Extend runner",
                "description": (
                    "Create a file `a.py` for the new stage.\n"
                    "Also `b.py` raises TypeError on empty input."
                ),
            }
            self.assertEqual(check_issue_premise(issue, tmp), ["b.py"])

    def test_existing_file_never_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "gcd.py").write_text("def gcd(a, b):\n    ...\n", encoding="utf-8")
            issue = {"title": "Fix", "description": "`gcd.py` crashes on negative input"}
            self.assertEqual(check_issue_premise(issue, tmp), [])


class TestMarkCompletedClearsVerification(unittest.TestCase):
    """P1-a: a completed record must not carry a prior attempt's failure."""

    def test_completed_clears_stale_verification_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="9", issue_identifier="ISSUE-9")
            reg.mark_failed_with_reason(
                "9", "premise_not_met: agent declared the issue cannot be completed"
            )
            record = reg.get("9")
            assert record is not None
            self.assertEqual(record.verification_status, "failed")

            record = reg.mark_completed("9")
            assert record is not None
            self.assertEqual(record.status, IssueStatus.COMPLETED)
            self.assertIsNone(record.verification_status)
            self.assertIsNone(record.verification_output)
            self.assertIsNone(record.last_hook_error)

            # Persisted to disk.
            reloaded = IssueRegistry(Path(tmp) / "r.json").get("9")
            assert reloaded is not None
            self.assertIsNone(reloaded.verification_status)


class TestMarkCompletedPreservesPassed(unittest.TestCase):
    """P1-a refinement: only failure-valued verification fields are cleared.

    The success path persists ``verification_status="passed"`` just before
    mark_completed; dashboards read it afterwards, so a blanket wipe broke
    the verification display.
    """

    def test_completed_preserves_passed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="12", issue_identifier="ISSUE-12")
            reg.update_report(
                "12",
                verification_status="passed",
                verification_output="9 passed in 0.1s",
            )
            record = reg.mark_completed("12")
            assert record is not None
            self.assertEqual(record.status, IssueStatus.COMPLETED)
            self.assertEqual(record.verification_status, "passed")
            self.assertEqual(record.verification_output, "9 passed in 0.1s")

    def test_completed_preserves_passed_but_clears_stale_hook_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="13", issue_identifier="ISSUE-13")
            reg.update_report("13", verification_status="passed")
            record = reg.get("13")
            assert record is not None
            record.last_hook_error = "stale hook noise"
            reg._save()

            record = reg.mark_completed("13")
            assert record is not None
            self.assertEqual(record.verification_status, "passed")
            self.assertIsNone(record.last_hook_error)


class TestReviewSalvageGate(unittest.TestCase):
    """P1-a follow-up: salvageable completions are detected via
    ``session_end_reason`` (verification fields are cleared on the
    COMPLETED transition), so ``issue review --reject`` still queues a
    retry for salvaged runs.
    """

    def test_reject_salvaged_completion_queues_retry(self) -> None:
        from unittest import mock

        from orchestratord.cli.issue import _run_review

        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "r.json"
            reg = IssueRegistry(registry_path)
            reg.register(issue_id="21", issue_identifier="ISSUE-21")
            reg.mark_failed_with_reason("21", "stagnation")
            reg.mark_completed("21")
            reg.update_report(
                "21",
                session_end_reason="salvaged_after_stagnation",
                session_end_summary="salvaged commit abcdef123456",
            )
            args = SimpleNamespace(
                id="21", approve=False, reject=True, feedback="wrong approach"
            )
            with mock.patch(
                "orchestratord.cli.issue._write_control", return_value=0
            ) as write_control:
                rc = _run_review(registry_path, args, workspace_root=tmp)
            self.assertEqual(rc, 0)
            write_control.assert_called_once()
            self.assertEqual(write_control.call_args.args[0], "review_retry")

    def test_reject_plain_completion_is_refused(self) -> None:
        from unittest import mock

        from orchestratord.cli.issue import _run_review

        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "r.json"
            reg = IssueRegistry(registry_path)
            reg.register(issue_id="22", issue_identifier="ISSUE-22")
            reg.mark_completed("22")
            reg.update_report("22", session_end_reason="success")
            args = SimpleNamespace(
                id="22", approve=False, reject=True, feedback="nope"
            )
            with mock.patch(
                "orchestratord.cli.issue._write_control", return_value=0
            ) as write_control:
                rc = _run_review(registry_path, args, workspace_root=tmp)
            self.assertEqual(rc, 1)
            write_control.assert_not_called()


class TestMarkPendingReleasesClaim(unittest.TestCase):
    """P1-b: a shutdown-interrupted run returns the issue to dispatchable PENDING."""

    def test_mark_pending_returns_to_dispatchable_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            reg.register(issue_id="11", issue_identifier="ISSUE-11")
            reg.mark_running("11")
            record = reg.mark_pending("11")
            assert record is not None
            self.assertEqual(record.status, IssueStatus.PENDING)
            self.assertEqual(record.pause_reason, "")

    def test_mark_pending_unknown_id_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            self.assertIsNone(reg.mark_pending("nope"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
