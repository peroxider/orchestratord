"""Tests for the SESSION_COMPLETE experience hook (断链守护).

The hook must: append exactly one friction score line per completion,
gate rules distillation at the strict threshold and learnings at the
lenient one (with the rolling-percentile gate degrading to the absolute
threshold on small samples), and never let consumer failures leak.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestratord.config.schema import ExperienceConfig
from orchestratord.telemetry.experience_hook import on_session_complete
from orchestratord.telemetry.friction import FrictionSignals


def _exp(**kw) -> ExperienceConfig:
    base = dict(
        enabled=True,
        friction_threshold_distill=60,
        friction_threshold_learnings=40,
        percentile_window=3,
    )
    base.update(kw)
    return ExperienceConfig(**base)


def _session(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        session_id="sess-1",
        run_id="run-1",
        backend_name="claude",
        workspace=SimpleNamespace(path=str(tmp_path)),
        subject=SimpleNamespace(id="ISSUE-7"),
    )


def _friction_path(tmp_path):
    return tmp_path / ".reports" / "friction.jsonl"


@pytest.fixture(autouse=True)
def _wire(monkeypatch, tmp_path):
    """Stub the config source and signal collection; tests control the
    score via monkeypatched ``score`` where needed."""
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook._config",
        lambda session: _exp(),
    )
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook.collect_signals",
        lambda session_id, **kw: FrictionSignals(retry_count=1),
    )


def test_disabled_config_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook._config",
        lambda session: None,
    )
    asyncio.run(on_session_complete(_session(tmp_path), {"duration_ms": 1000}))
    assert not _friction_path(tmp_path).exists()


def test_appends_single_score_line(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook.score", lambda s, b: 42
    )
    asyncio.run(on_session_complete(_session(tmp_path), {"duration_ms": 1500}))

    lines = _friction_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["session_id"] == "sess-1"
    assert entry["run_id"] == "run-1"
    assert entry["issue_id"] == "ISSUE-7"
    assert entry["backend"] == "claude"
    assert entry["duration_s"] == pytest.approx(1.5)
    assert entry["score"] == 42


def test_low_score_triggers_no_consumer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook.score", lambda s, b: 5
    )
    called = {"distill": 0, "learnings": 0}

    async def fake_distill(session, config):
        called["distill"] += 1
        return 0

    async def fake_learnings(*_a, **_kw):
        called["learnings"] += 1
        return None

    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_distill", fake_distill
    )
    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_learnings", fake_learnings
    )

    asyncio.run(on_session_complete(_session(tmp_path), {"duration_ms": 0}))
    assert called == {"distill": 0, "learnings": 0}


def test_high_score_triggers_both_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook.score", lambda s, b: 90
    )
    called = {"distill": 0, "learnings": 0}

    async def fake_distill(session, config):
        called["distill"] += 1
        return 2

    async def fake_learnings(*_a, **_kw):
        called["learnings"] += 1
        return tmp_path / "learning.md"

    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_distill", fake_distill
    )
    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_learnings", fake_learnings
    )

    asyncio.run(on_session_complete(_session(tmp_path), {"duration_ms": 0}))
    assert called == {"distill": 1, "learnings": 1}


def test_small_history_degrades_to_absolute_threshold(tmp_path, monkeypatch):
    # Below the strict threshold but above the lenient one: only
    # learnings run, even with a tiny (sub-window) history.
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook.score", lambda s, b: 50
    )
    called = {"distill": 0, "learnings": 0}

    async def fake_distill(session, config):
        called["distill"] += 1
        return 0

    async def fake_learnings(*_a, **_kw):
        called["learnings"] += 1
        return None

    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_distill", fake_distill
    )
    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_learnings", fake_learnings
    )

    asyncio.run(on_session_complete(_session(tmp_path), {"duration_ms": 0}))
    assert called == {"distill": 0, "learnings": 1}


def test_full_history_percentile_gate_blocks_distill(tmp_path, monkeypatch):
    # History has percentile_window high-score samples; score 60 passes
    # the absolute strict threshold but sits below p75([90,90,90])=90,
    # so the rolling gate blocks distillation.
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook.score", lambda s, b: 60
    )
    path = _friction_path(tmp_path)
    path.parent.mkdir(parents=True)
    for _ in range(3):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"score": 90, "duration_s": 10}) + "\n")

    called = {"distill": 0}

    async def fake_distill(session, config):
        called["distill"] += 1
        return 0

    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_distill", fake_distill
    )

    asyncio.run(on_session_complete(_session(tmp_path), {"duration_ms": 0}))
    assert called["distill"] == 0


def test_consumer_failure_does_not_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestratord.telemetry.experience_hook.score", lambda s, b: 90
    )

    async def broken(session, config):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_distill", broken
    )
    monkeypatch.setattr(
        "orchestratord.experience.pipeline.run_learnings", broken
    )

    # Must not raise despite both consumers exploding.
    asyncio.run(on_session_complete(_session(tmp_path), {"duration_ms": 0}))
    # And the score rollup still landed before the consumers ran.
    assert _friction_path(tmp_path).exists()
