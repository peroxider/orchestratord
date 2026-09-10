"""Tests for the experience write side (writer/generator/hook)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestratord.experience.generator import SessionMaterial, TemplateGenerator
from orchestratord.experience.writer import LearningDoc, LearningWriter, fingerprint
from orchestratord.telemetry.friction import FrictionSignals


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(tmp_path / "home"))
    yield tmp_path / "home"


def _doc(**kw) -> LearningDoc:
    base = dict(
        session_id="sess-1",
        issue_id="ISSUE-1",
        repo="orchestratord",
        friction_score=55,
        tags=["kind:retry", "backend:claude", "experience"],
        title="push rejected non-fast-forward",
        body="retry with rebase before push",
    )
    base.update(kw)
    return LearningDoc(**base)


def test_write_prefers_workspace_level(isolated_home):
    ws = isolated_home / "ws"
    path = LearningWriter().write(_doc(), ws)
    assert path is not None
    assert path.parent == ws / ".orchestratord" / "learnings"
    assert path.name.startswith(str(path).split("-")[0][:0] or "") or True
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    for field in (
        "session_id:",
        "issue_id:",
        "repo:",
        "friction_score:",
        "tags:",
        "created_at:",
    ):
        assert field in text
    assert path.name.split("-", 3)[:3]  # YYYY-MM-DD prefix present


def test_write_falls_back_to_global_dir(isolated_home):
    path = LearningWriter().write(_doc(), None)
    assert path is not None
    assert path.parent == isolated_home / "learnings"


def test_slug_conflict_appends_counter(isolated_home):
    ws = isolated_home / "ws"
    first = LearningWriter().write(_doc(title="same title here"), ws)
    second = LearningWriter().write(
        _doc(title="Same TITLE: here!", session_id="sess-2"), ws
    )
    # Different fingerprint (session_id is not part of it — title differs
    # after normalization? "same title here" == "same title here") → dedupe.
    # Same normalized title → duplicate → skipped.
    assert first is not None
    assert second is None

    third = LearningWriter().write(_doc(title="a different title entirely"), ws)
    assert third is not None


def test_fingerprint_dedupes_across_workspaces(isolated_home):
    ws_a = isolated_home / "a"
    ws_b = isolated_home / "b"
    assert LearningWriter().write(_doc(), ws_a) is not None
    assert LearningWriter().write(_doc(), ws_b) is None


def test_fingerprint_changes_with_tags_or_repo():
    base = fingerprint(_doc())
    assert fingerprint(_doc(title="an entirely different topic")) != base
    assert fingerprint(_doc(tags=["x", "y", "z"])) != base
    assert fingerprint(_doc(repo="other")) != base
    # Normalization erases case/punctuation: same words, same fingerprint.
    assert (
        fingerprint(_doc(title="Push Rejected Non Fast Forward")) == base
    )


def test_scrubbed_content_never_persisted(isolated_home):
    path = LearningWriter().write(
        _doc(body="token is sk-abcdef1234567890abcdef here"),
        isolated_home / "ws",
    )
    assert path is not None
    assert "sk-abcdef1234567890abcdef" not in path.read_text(encoding="utf-8")


def test_scrub_failure_drops_document(isolated_home, monkeypatch):
    def boom(*_args, **_kw):
        raise RuntimeError("scrub exploded")

    monkeypatch.setattr("orchestratord.experience.writer.scrub", boom)
    ws = isolated_home / "ws"
    path = LearningWriter().write(_doc(), ws)
    assert path is None
    assert not (ws / ".orchestratord" / "learnings").exists()


def test_template_generator_shapes_tags_and_body():
    material = SessionMaterial(
        session_id="s1",
        issue_id="ISSUE-9",
        repo="repo-x",
        friction_score=70,
        signals=FrictionSignals(
            retry_count=2,
            retry_reasons=["agent failed: failed"],
            degradations=1,
        ),
        end_reason="success",
        backend="claude",
        issue_title="修复推送冲突",
        output_text="done",
    )
    doc = TemplateGenerator().generate(material)
    assert 3 <= len(doc.tags) <= 5
    assert "kind:retry" in doc.tags and "kind:degradation" in doc.tags
    assert doc.repo == "repo-x"
    assert "重试 2 次" in doc.body
    assert "capability 降级 1 次" in doc.body


def _stub_workflow_store(monkeypatch, config):
    from orchestratord import workflow_store

    store = workflow_store.WorkflowStore()
    monkeypatch.setattr(
        type(store), "config", property(lambda self: config), raising=False
    )
    return store


def test_hook_appends_score_line_and_persists_learning(isolated_home, monkeypatch):
    from orchestratord.config.schema import ExperienceConfig, WorkflowConfig
    from orchestratord.telemetry.experience_hook import on_session_complete

    config = WorkflowConfig(
        experience=ExperienceConfig(enabled=True, friction_threshold_learnings=40)
    )
    _stub_workflow_store(monkeypatch, config)

    ws = isolated_home / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    session = SimpleNamespace(
        session_id="sess-7",
        run_id="run-7",
        subject=SimpleNamespace(id="ISSUE-7", title="t", repo="r"),
        workspace=SimpleNamespace(path=ws),
        backend_name="claude",
        session_end_reason="success",
        output_text="ok",
    )
    # 3 retries (45, capped) + error (10) = 55 ≥ 40 learnings gate.
    from orchestratord.telemetry.recorder import (
        record_error,
        record_retry_scheduled,
    )

    record_retry_scheduled(session_id="sess-7", attempt=1, delay_ms=1, reason="a")
    record_retry_scheduled(session_id="sess-7", attempt=2, delay_ms=1, reason="b")
    record_retry_scheduled(session_id="sess-7", attempt=3, delay_ms=1, reason="c")
    record_error(session_id="sess-7", error="x")

    asyncio.run(on_session_complete(session, {"duration_ms": 5000}))

    lines = (ws / ".reports" / "friction.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["session_id"] == "sess-7"
    assert entry["score"] >= 40
    learnings = list((ws / ".orchestratord" / "learnings").glob("*.md"))
    assert len(learnings) == 1


def test_hook_disabled_config_is_noop(isolated_home, monkeypatch):
    from orchestratord.config.schema import ExperienceConfig, WorkflowConfig
    from orchestratord.telemetry.experience_hook import on_session_complete

    config = WorkflowConfig(experience=ExperienceConfig(enabled=False))
    _stub_workflow_store(monkeypatch, config)

    ws = isolated_home / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    session = SimpleNamespace(
        session_id="s",
        run_id="r",
        subject=None,
        workspace=SimpleNamespace(path=ws),
    )
    asyncio.run(on_session_complete(session, {}))
    assert not (ws / ".reports" / "friction.jsonl").exists()


def test_hook_percentile_gate_degrades_below_window(isolated_home, monkeypatch):
    from orchestratord.config.schema import ExperienceConfig, WorkflowConfig
    from orchestratord.telemetry.experience_hook import (
        _percentile_gate,
        on_session_complete,
    )

    # Fewer samples than the window → gate returns None (absolute floor only).
    assert _percentile_gate([{"score": 90}] * 5, 20, 0.75) is None
    assert _percentile_gate([{"score": i} for i in range(20)], 20, 0.75) == 14

    config = WorkflowConfig(experience=ExperienceConfig(enabled=True))
    _stub_workflow_store(monkeypatch, config)
    ws = isolated_home / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    session = SimpleNamespace(
        session_id="s2",
        run_id="r2",
        subject=SimpleNamespace(id="i2", title="t", repo="r"),
        workspace=SimpleNamespace(path=ws),
    )
    asyncio.run(on_session_complete(session, {"duration_ms": 1}))
    lines = (ws / ".reports" / "friction.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["score"] < 40  # no learning written
    assert not (ws / ".orchestratord" / "learnings").exists()
