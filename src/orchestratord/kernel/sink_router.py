"""SinkRouter — 进度 sink 组装的机制段（DESIGN §5 机制职责表第 8 行）。

从 orchestrator.py ``_build_session_sink`` 迁入的基础组装：ToolContext
进度 sink + 复合容器 + AsciicastSink 挂载。IM/channel 类 sink 由宿主经
``KernelHooks.on_session_sink_build``（DESIGN §4.7）挂载，不在机制域内。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config.schema import WorkflowConfig

logger = logging.getLogger(__name__)


def build_base_session_sink(
    *,
    task_id: str,
    workflow: WorkflowConfig,
    progress_context: Any = None,
) -> Any:
    """Build a fresh :class:`CompositeProgressSink` for one session.

    The returned sink is bound to ``task_id`` and owns a private
    :class:`ToolContextProgressSink` instance. Two sinks built for
    different task ids share the underlying ``ToolContext`` (so
    progress stages land in the right place) but have independent
    phase counters, eliminating the legacy single-instance
    cross-talk.

    Future issues (PR review auto-fix sink, retry label sink)
    can register additional sinks on the returned composite via
    :meth:`CompositeProgressSink.add` without touching
    :class:`AgentRunner` or ``progress_reporter.py``.
    """
    from ..sinks.progress import (
        CompositeProgressSink,
        ToolContextProgressSink,
    )

    inner = ToolContextProgressSink(
        task_id=task_id,
        workflow_phases=workflow.agent.phases,
        fallback_to_phase_step=bool(workflow.agent.fallback_to_phase_step),
        context=progress_context,
    )
    return CompositeProgressSink([inner])


def attach_asciicast_sink(
    composite: Any,
    *,
    capture: Any,
    task_id: str,
    phases: Any,
) -> None:
    """F-REC: attach an :class:`AsciicastSink` when a capture handle is wired.

    Typically wired by the report CLI or ``report_writer.write``
    dual-write so phase / session markers land in the .cast.
    Defensive try/except — recording failures must never block
    the live orchestrator.
    """
    if capture is None:
        return
    try:
        from ..sinks.asciicast import AsciicastSink

        phases_total = len(phases) if phases else None
        composite.add(
            AsciicastSink(
                capture,
                task_id=task_id,
                phases_total=phases_total,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "asciicast sink attach failed (task_id=%s): %s",
            task_id,
            exc,
        )
