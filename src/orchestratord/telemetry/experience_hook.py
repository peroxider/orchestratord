"""SESSION_COMPLETE experience hook (DESIGN_EXPERIENCE_LOOP.md §3/§5/§8).

One trigger, two consumers: score the finished session's friction,
append the score to the workspace ``.reports/friction.jsonl`` rollup
(Phase-C calibration data), then apply the dual thresholds — rules
distillation at the strict gate, learnings persistence at the lenient
one. Every step is best-effort: this hook must never fail the run.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from orchestratord.telemetry.friction import (
    FrictionSignals,
    _percentile,
    collect_signals,
    score,
)

logger = logging.getLogger(__name__)


def _config(session: Any) -> Any:
    from orchestratord.workflow_store import get_workflow_store

    config = get_workflow_store().config
    exp = getattr(config, "experience", None) if config is not None else None
    if exp is None or not exp.enabled:
        return None
    return exp


def _workspace_root(session: Any) -> Path | None:
    workspace = getattr(session, "workspace", None)
    path = getattr(workspace, "path", None)
    return Path(path) if path else None


def _friction_path(workspace_root: Path | None) -> Path:
    if workspace_root is None:
        # No workspace context: fall back to the global home dir so the
        # score rollup still accumulates for Phase-C calibration.
        base = os.environ.get(
            "ORCHESTRATORD_HOME", str(Path.home() / ".orchestratord")
        )
        return Path(base) / "reports" / "friction.jsonl"
    return workspace_root / ".reports" / "friction.jsonl"


def _load_history(path: Path, window: int) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-window:]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _baseline(history: list[dict]) -> dict[str, float] | None:
    durations = sorted(
        float(entry.get("duration_s", 0) or 0) for entry in history
    )
    durations = [d for d in durations if d > 0]
    if not durations:
        return None
    baseline: dict[str, float] = {}
    p50 = _percentile(durations, 0.50)
    p95 = _percentile(durations, 0.95)
    if p50:
        baseline["p50"] = p50
    if p95:
        baseline["p95"] = p95
    return baseline or None


def _percentile_gate(
    history: list[dict], window: int, q: float
) -> float | None:
    """Rolling gate; None when fewer than ``window`` samples exist."""
    if len(history) < window:
        return None
    scores = sorted(float(entry.get("score", 0) or 0) for entry in history)
    return _percentile(scores, q)


def _append_score_line(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("failed to append friction score line", exc_info=True)


async def on_session_complete(session: Any, payload: dict) -> None:
    exp = _config(session)
    if exp is None:
        return

    session_id = (
        getattr(session, "session_id", None)
        or getattr(session, "run_id", None)
        or ""
    )
    workspace_root = _workspace_root(session)
    duration_s = float(payload.get("duration_ms", 0) or 0) / 1000.0

    signals: FrictionSignals = collect_signals(
        session_id, duration_s=duration_s
    )
    friction_path = _friction_path(workspace_root)
    history = _load_history(friction_path, exp.percentile_window)
    friction_score = score(signals, _baseline(history))

    _append_score_line(
        friction_path,
        {
            "ts": time.time(),
            "session_id": session_id,
            "run_id": getattr(session, "run_id", None) or "",
            "issue_id": str(
                getattr(
                    getattr(session, "subject", None)
                    or getattr(session, "issue", None),
                    "id",
                    None,
                )
                or ""
            ),
            "backend": getattr(session, "backend_name", None) or "",
            "duration_s": duration_s,
            "score": friction_score,
        },
    )

    # Strict gate: rules distillation (BatchedLLMJudge — the expensive side).
    p75 = _percentile_gate(history, exp.percentile_window, 0.75)
    if friction_score >= exp.friction_threshold_distill and (
        p75 is None or friction_score >= p75
    ):
        try:
            from orchestratord.config.schema import WorkflowConfig
            from orchestratord.workflow_store import get_workflow_store

            config = get_workflow_store().config or WorkflowConfig()
            distilled = await run_distill(session, config)
            if distilled:
                logger.info(
                    "experience loop distilled %d rule(s) session=%s",
                    distilled,
                    session_id,
                )
        except Exception:
            logger.warning("rules distillation failed", exc_info=True)

    # Lenient gate: situational learnings (one cheap write).
    p50 = _percentile_gate(history, exp.percentile_window, 0.50)
    if friction_score >= exp.friction_threshold_learnings and (
        p50 is None or friction_score >= p50
    ):
        try:
            from orchestratord.experience.pipeline import run_learnings

            path = await run_learnings(
                session, workspace_root, exp, signals, friction_score
            )
            if path is not None:
                logger.info("learning persisted: %s", path)
        except Exception:
            logger.warning("learnings persistence failed", exc_info=True)


async def run_distill(session: Any, config: Any) -> int:
    from orchestratord.experience.pipeline import run_distill as _run

    return await _run(session, config)


__all__ = ["on_session_complete"]
