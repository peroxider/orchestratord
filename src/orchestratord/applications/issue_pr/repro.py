"""复现门（repro-first gate）— issue→PR 应用的业务策略（DESIGN §4.2）。

C2b 之后仍残留于宿主 ``_launch_issue`` 执行链的复现门业务段，随
dispatch-loop 切片迁入应用侧。模块顶层只 import 低层业务模块
（repro_gate）与共享基础设施，不 import orchestrator / orchestration_subsystem。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from orchestratord.repro_gate import (
    ReproGateResult,
    append_repro_hint,
    build_repro_prompt,
    evaluate_repro_gate,
    format_repro_gate_comment,
)
from orchestratord.session_state import AgentSession

logger = logging.getLogger(__name__)


def repro_gate_applies(host: Any, session: AgentSession) -> bool:
    """The gate only fronts fresh issue runs (not retries of other
    run kinds), only when enabled, and — when ``labels`` is
    configured — only for issues carrying one of those labels."""
    config = host.workflow.agent.repro_first
    if not config.enabled or session.run_kind != "issue":
        return False
    if config.labels:
        issue_labels = {
            label.strip().lower() for label in (getattr(session.issue, "labels", None) or [])
        }
        wanted = {label.strip().lower() for label in config.labels}
        if not issue_labels & wanted:
            return False
    return True


async def run_repro_gate(host: Any, session: AgentSession, progress_sink: Any) -> bool:
    """Run the reproduction stage; True means "bug demonstrated,
    proceed to the fix stage".

    On a closed gate the issue is marked FAILED with a
    "cannot reproduce" report posted to the tracker, mirroring the
    empty-branch failure path (no MR is opened).
    """
    issue = session.issue
    config = host.workflow.agent.repro_first
    session.run_kind = "repro"
    session.prompt_override = build_repro_prompt(issue)
    repro_timeout_seconds = config.timeout_ms / 1000.0
    session.timeout_deadline_at = time.time() + repro_timeout_seconds
    logger.info("Issue %s: repro-first gate starting", issue.id)
    timed_out = False
    try:
        await asyncio.wait_for(
            host.agent_runner.run(
                session,
                host.workflow,
                status_dashboard=host.status_dashboard,
                # The repro stage has its own executable completion
                # contract below. Passing the tracker here makes the
                # generic runner continue while the issue is still open,
                # even after the repro artifacts are complete.
                tracker=None,
                comment_tracker=host.tracker,
                clarification_resolver=host._clarification_resolver,
                progress_reporter=progress_sink,
                diagnostics_callback=host._update_run_diagnostics,
            ),
            timeout=repro_timeout_seconds,
        )
    except asyncio.TimeoutError:
        timed_out = True
        logger.warning("Issue %s: repro stage timed out", issue.id)

    result = ReproGateResult(verdict="missing")
    if not timed_out:
        result = await evaluate_repro_gate(
            session.workspace.path,
            timeout_ms=config.command_timeout_ms,
        )

    if result.proceed:
        assert result.command is not None
        logger.info(
            "Issue %s: reproduction established (%s) — opening fix stage",
            issue.id,
            result.command,
        )
        session.repro_command = result.command
        append_repro_hint(session.workspace.path, result.command)
        # Reset per-run state so the fix stage gets a clean session
        # (mirrors the pipeline mode's between-stage reset).
        session.turn_count = 0
        session.status = "running"
        session.output_text = ""
        session.session_end_reason = None
        session.session_end_summary = ""
        session.run_id = None
        session.consecutive_429_count = 0
        session.rate_limit_pending_turn = None
        session.prompt_override = None
        session.run_kind = "issue"
        return True

    verdict = "repro_stage_timeout" if timed_out else result.verdict
    logger.warning(
        "Issue %s: repro-first gate closed (verdict=%s) — marking FAILED "
        "without attempting a fix",
        issue.id,
        verdict,
    )
    session.status = "failed"
    session.session_end_reason = "not_reproducible"
    session.session_end_summary = f"repro gate closed: {verdict}"
    host._registry.mark_failed_with_reason(
        issue.id or "",
        f"not_reproducible ({verdict}): the described behavior could not "
        "be demonstrated; no fix attempted, no PR created.",
    )
    try:
        await host.tracker.create_comment(
            issue.id or "",
            format_repro_gate_comment(issue, result),
        )
    except Exception:
        logger.warning(
            "Issue %s: failed to post repro-gate comment",
            issue.id,
            exc_info=True,
        )
    await host._sync_tracker_issue_state(issue.id or "", "failed")
    host.status_dashboard.on_session_complete(issue.id or "")
    host._state.completed.add(issue.id or "")
    host._state.failed.add(issue.id or "")
    return False


__all__ = ["repro_gate_applies", "run_repro_gate"]
