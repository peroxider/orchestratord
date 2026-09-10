"""Experience pipeline — the two write sides behind the friction gate.

``run_distill`` feeds write side B (review rules into
``workflow.rules.yaml`` via the existing RuleEngine); ``run_learnings``
feeds write side A (a structured situational document into the
two-level learnings layout). Both are called from the
``SESSION_COMPLETE`` experience hook only after the friction thresholds
pass, and both are best-effort: failures surface as warnings, never as
run failures.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orchestratord.config.schema import ExperienceConfig
from orchestratord.telemetry.friction import FrictionSignals

from .generator import SessionMaterial, TemplateGenerator
from .writer import LearningWriter

logger = logging.getLogger(__name__)

_RULES_MARKER = "## Extracted Rules"


def _session_id(session: Any) -> str:
    return (
        getattr(session, "session_id", None)
        or getattr(session, "run_id", None)
        or ""
    )


def _issue(session: Any) -> Any:
    return getattr(session, "subject", None) or getattr(session, "issue", None)


def _repo(session: Any, workspace_root: Path | None) -> str:
    subject = _issue(session)
    repo = ""
    if subject is not None:
        repo = str(
            getattr(subject, "repo", None)
            or getattr(subject, "repo_name", None)
            or ""
        )
    if not repo and workspace_root:
        repo = Path(workspace_root).name
    return repo


async def run_distill(session: Any, config: Any) -> int:
    """Extract rules from this session's output into the rules store.

    Only sessions whose reply carries an ``## Extracted Rules`` section
    (prompted by the review-feedback template or an explicit convention)
    produce candidates; everything else is a no-op.
    """
    from orchestratord.rules_learner import RuleEngine
    from orchestratord.workflow_store import get_workflow_store

    output = getattr(session, "output_text", "") or ""
    if _RULES_MARKER not in output:
        return 0
    workflow_path = get_workflow_store().workflow_path
    rules_path = RuleEngine.get_rules_path(config, workflow_path)
    if not rules_path:
        return 0
    engine = RuleEngine()
    return await engine.apply(
        output,
        rules_path,
        max_rules=config.rules.max_rules,
        min_confidence=config.rules.min_confidence,
        source=f"experience session {_session_id(session)}",
    )


async def run_learnings(
    session: Any,
    workspace_root: Path | None,
    exp_config: ExperienceConfig,
    signals: FrictionSignals,
    friction_score: int,
) -> Path | None:
    """Generate and persist one situational learning document."""
    subject = _issue(session)
    issue_id = str(
        getattr(subject, "id", None) or getattr(subject, "identifier", None) or ""
    )
    feedback_body = getattr(session, "feedback_commit_body", None)
    feedback_items: list[Any] = (
        [{"body": feedback_body}] if feedback_body else []
    )
    material = SessionMaterial(
        session_id=_session_id(session),
        issue_id=issue_id,
        repo=_repo(session, workspace_root),
        friction_score=friction_score,
        signals=signals,
        end_reason=str(getattr(session, "session_end_reason", "") or ""),
        backend=str(getattr(session, "backend_name", "") or ""),
        issue_title=str(getattr(subject, "title", None) or ""),
        feedback_items=feedback_items,
        output_text=str(getattr(session, "output_text", "") or ""),
    )
    doc = TemplateGenerator().generate(material)
    writer = LearningWriter(allowlist=exp_config.privacy_allowlist)
    try:
        return writer.write(doc, workspace_root)
    except Exception:
        # Fail-closed: scrub or write errors drop the artifact instead of
        # persisting unscrubbed content.
        logger.warning(
            "learning document dropped (scrub/write failure)", exc_info=True
        )
        return None


__all__ = ["run_distill", "run_learnings"]
