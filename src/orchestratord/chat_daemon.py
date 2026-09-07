"""Chat daemon wiring: real BackendRunner behind the dispatcher seam (§6.1d).

:class:`~orchestratord.chat_dispatcher.ChatDispatcher` deliberately takes a
``runner_invoke`` callable so it stays testable without agent configuration.
This module is the production half of that seam:

* :func:`progress_event_adapter` maps the ``run_task`` ``ProgressEvent``
  stream back onto the synchronous ``progress_reporter`` protocol the
  :class:`~orchestratord.chat_bridge.ChatMessageBridge` implements
  (``run_task`` wraps the raw callback in ``_TaskProgressBridge``, which
  emits ``ProgressEvent`` objects rather than calling sink methods).
* :func:`build_chat_runner_invoke` resolves the backend name — the
  session's ``agent_id`` → ``agents.provider``, else
  ``ORCHESTRATORD_CHAT_BACKEND`` — builds a default
  :class:`~orchestratord.config.schema.WorkflowConfig` rooted at
  ``ORCHESTRATORD_CHAT_WORKSPACE_ROOT`` (cwd by default) and runs the task.
* :func:`start_chat_daemon` spawns the claim loop as an asyncio task for
  the app lifespan (gated by ``ORCHESTRATORD_CHAT_DAEMON=1``).

Unresolvable backend names raise before any bridge callback fires; the
dispatcher converts that into ``status="failed"`` so the failure is visible
on the session rather than silently re-queued.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from orchestratord.chat_dispatcher import ChatDispatcher, RunnerInvoke
from orchestratord.db.engine import build_session_factory

logger = logging.getLogger(__name__)


def progress_event_adapter(bridge: Any) -> Callable[[Any], None]:
    """Translate ``ProgressEvent`` callbacks into bridge sink calls."""

    from orchestratord.agent.task import ProgressEventKind

    def on_event(event: Any) -> None:
        kind = event.kind
        if kind is ProgressEventKind.TEXT:
            bridge.on_text(event.text)
        elif kind is ProgressEventKind.TEXT_DELTA:
            bridge.on_text_delta(event.text)
        elif kind is ProgressEventKind.TOOL_CALL:
            bridge.on_tool_call(event.tool_name, event.call_id)
        elif kind is ProgressEventKind.TOOL_RESULT:
            bridge.on_tool_result(event.call_id)
        elif kind is ProgressEventKind.TURN_COMPLETE:
            bridge.on_turn_complete(None, None)
        elif kind is ProgressEventKind.SESSION_COMPLETE:
            bridge.on_session_complete(None, None)
        elif kind is ProgressEventKind.ERROR:
            bridge.on_error(event.message)

    return on_event


async def _resolve_backend_name(session_factory: Any, session_row: Any) -> str:
    """Session's agent provider, else ``ORCHESTRATORD_CHAT_BACKEND``."""
    from orchestratord.db import models as orm

    agent_id = getattr(session_row, "agent_id", None)
    if agent_id is not None:
        async with session_factory() as db:
            agent = await db.get(orm.Agent, agent_id)
            if agent is not None and agent.provider:
                return agent.provider
    env_name = os.environ.get("ORCHESTRATORD_CHAT_BACKEND")
    if env_name:
        return env_name
    raise LookupError(
        "no chat backend resolved: session has no agent and "
        "ORCHESTRATORD_CHAT_BACKEND is not set"
    )


def build_chat_runner_invoke(session_factory: Any) -> RunnerInvoke:
    """Build the production ``runner_invoke`` for :class:`ChatDispatcher`."""

    async def invoke(session_row: Any, prompt: str, bridge: Any) -> None:
        from orchestratord.agent.task import AgentTask
        from orchestratord.backend_registry import resolve_backend
        from orchestratord.backend_runner import BackendRunner
        from orchestratord.config.schema import WorkflowConfig

        backend_name = await _resolve_backend_name(session_factory, session_row)
        root = os.environ.get("ORCHESTRATORD_CHAT_WORKSPACE_ROOT") or os.getcwd()
        backend = resolve_backend(backend_name)
        config = WorkflowConfig()
        config.workspace.root = root
        runner = BackendRunner(backend, config.agent, config.sandbox, config.workspace)
        task = AgentTask(
            id=f"chat-{session_row.id}",
            kind="chat",
            # Deterministic per chat session so resumable backends can pick
            # up the same conversation across turns.
            conversation_id=str(session_row.id),
            title=prompt[:80] or "chat session",
            description=prompt,
            workspace_path=root,
            prompt_override=prompt,
        )
        await runner.run_task(task, progress_callback=progress_event_adapter(bridge))

    return invoke


def start_chat_daemon(
    session_factory: Any = None,
    interval: float = 5.0,
) -> ChatDispatcher:
    """Create the dispatcher and spawn its claim loop as an asyncio task."""
    factory = session_factory if session_factory is not None else build_session_factory()
    dispatcher = ChatDispatcher(
        factory, build_chat_runner_invoke(factory), interval=interval
    )
    dispatcher._loop_task = asyncio.create_task(
        dispatcher.run_forever(), name="chat-dispatcher"
    )
    return dispatcher


async def stop_chat_daemon(dispatcher: ChatDispatcher) -> None:
    """Signal the loop to stop and await (or cancel) its task."""
    dispatcher.stop()
    task = getattr(dispatcher, "_loop_task", None)
    if task is not None and not task.done():
        # A dispatch in flight may block the loop for a while; give it a
        # short grace period to notice the flag, then cancel rather than
        # hang shutdown.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            return
        except asyncio.TimeoutError:
            task.cancel()
        except Exception:
            logger.debug("chat dispatcher loop ended with error", exc_info=True)
            return
        try:
            await task
        except asyncio.CancelledError:
            pass


__all__ = [
    "build_chat_runner_invoke",
    "progress_event_adapter",
    "start_chat_daemon",
    "stop_chat_daemon",
]
