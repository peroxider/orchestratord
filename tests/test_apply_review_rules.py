"""Tests for the resurrected _apply_review_rules (断链复活).

Exercises :meth:`IssuePrInterpretation._apply_review_rules` with a
dependency-stripped instance (``object.__new__`` style, matching
``tests/test_telemetry_aggregator.py``) and tmp-dir rules storage.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from orchestratord.applications.issue_pr.interpret import IssuePrInterpretation
from orchestratord.config.schema import RulesConfig, WorkflowConfig
from orchestratord.rules_learner import ExtractTracker, RuleStore

_REPLY = """The fix is committed.

## Extracted Rules
- [testing] always run pytest before push
  Body: never skip the test gate on follow-up commits
"""

_SHA = "abc123def4567890"


def _interp(workflow: WorkflowConfig) -> IssuePrInterpretation:
    interp = object.__new__(IssuePrInterpretation)
    interp.host = SimpleNamespace(workflow=workflow)
    return interp


def _session(**kw) -> SimpleNamespace:
    base = dict(
        output_text=_REPLY,
        feedback_commit_body=None,
        issue=SimpleNamespace(id="ISSUE-1"),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sync(sha: str | None = _SHA) -> SimpleNamespace:
    return SimpleNamespace(commit_sha=sha)


def _workflow(enabled: bool = True) -> WorkflowConfig:
    return WorkflowConfig(rules=RulesConfig(enabled=enabled))


def _rules_path(tmp_path) -> str:
    return str(tmp_path / "workflow.rules.yaml")


def _store(tmp_path):
    from orchestratord import workflow_store

    store = workflow_store.WorkflowStore()
    store._workflow_path = str(tmp_path / "workflow.yaml")
    return store


def test_extracts_and_consumes_sha(tmp_path):
    _store(tmp_path)
    rules_path = _rules_path(tmp_path)
    interp = _interp(_workflow())

    asyncio.run(interp._apply_review_rules(_session(), _sync()))

    rules = RuleStore.load(rules_path)
    assert len(rules["rules"]) == 1
    assert rules["rules"][0]["summary"] == "always run pytest before push"
    assert _SHA in ExtractTracker(rules_path).load()


def test_second_call_is_idempotent(tmp_path):
    _store(tmp_path)
    rules_path = _rules_path(tmp_path)
    interp = _interp(_workflow())

    sync = _sync()
    asyncio.run(interp._apply_review_rules(_session(), sync))
    asyncio.run(interp._apply_review_rules(_session(), sync))

    rules = RuleStore.load(rules_path)
    assert len(rules["rules"]) == 1


def test_rules_disabled_skips(tmp_path):
    _store(tmp_path)
    rules_path = _rules_path(tmp_path)
    interp = _interp(_workflow(enabled=False))

    asyncio.run(interp._apply_review_rules(_session(), _sync()))
    assert RuleStore.load(rules_path)["rules"] == []
    assert _SHA not in ExtractTracker(rules_path).load()


def test_missing_sync_result_is_noop(tmp_path):
    _store(tmp_path)
    interp = _interp(_workflow())

    asyncio.run(interp._apply_review_rules(_session(), None))
    assert _SHA not in ExtractTracker(_rules_path(tmp_path)).load()


def test_no_extracted_rules_section_does_not_consume_sha(tmp_path):
    _store(tmp_path)
    rules_path = _rules_path(tmp_path)
    interp = _interp(_workflow())

    asyncio.run(
        interp._apply_review_rules(_session(output_text="plain reply"), _sync())
    )
    assert RuleStore.load(rules_path)["rules"] == []
    assert _SHA not in ExtractTracker(rules_path).load()


def test_extraction_failure_does_not_consume_sha(tmp_path, monkeypatch):
    _store(tmp_path)
    rules_path = _rules_path(tmp_path)
    interp = _interp(_workflow())

    async def broken_apply(*_args, **_kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(
        "orchestratord.rules_learner.RuleEngine.apply", broken_apply
    )

    asyncio.run(interp._apply_review_rules(_session(), _sync()))
    assert _SHA not in ExtractTracker(rules_path).load()
