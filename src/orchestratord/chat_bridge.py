"""Chat message bridge: SPI runtime events → chat ``messages`` rows (§6.1d).

The chat timeline (``GET /api/sessions/{id}/messages``) renders folded
conversation turns, not raw SPI events. This bridge is the fold: it
implements the same synchronous ``progress_reporter`` protocol the
:class:`orchestratord.backend_runner.BackendRunner` event loop calls
(:class:`orchestratord.backend_runner._TaskProgressBridge` documents why the
callbacks are synchronous) and accumulates ``TEXT`` / ``TEXT_DELTA`` payloads
into per-turn assistant messages that land in the ``messages`` table.

Folding rules:

* ``TEXT`` and ``TEXT_DELTA`` append to the open turn's buffer. A turn is
  opened lazily on the first text of the turn and closed on
  ``TURN_COMPLETE`` / ``SESSION_COMPLETE`` — one assistant message per turn.
* ``TOOL_CALL`` / ``TOOL_RESULT`` are not persisted: the timeline stays
  prose-only (tool activity lives on the events stream §5.2.3 and the
  transcript dialog §5.5), but they are streamed to the realtime channel so
  the chat composer can surface live activity.
* ``on_error`` closes the open turn (flushing what arrived) so a failed run
  still leaves the partial answer on the timeline.

Every sink also publishes a best-effort frame on the ``chat.{session_id}``
realtime topic (``api/realtime.py``) so WebSocket clients render streaming
text without waiting for the turn flush; terminal frames
(``turn_complete`` / ``session_complete`` / ``error``) are published only
after the turn's rows are committed, so a client refetch triggered by the
frame never races an uncommitted insert.

DB writes are scheduled as asyncio tasks (one fresh session per flush via
``session_factory``) and awaited in :meth:`flush`, mirroring
``_TaskProgressBridge.flush``. Callback failures are logged and isolated —
a broken chat timeline must never fail agent execution.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from orchestratord.api.realtime import get_broker
from orchestratord.db.models import Message
from orchestratord.db.repository import Repositories

logger = logging.getLogger(__name__)


class ChatMessageBridge:
    """Fold runner progress callbacks into assistant ``messages`` rows."""

    def __init__(
        self,
        session_factory: Any,
        session_id: UUID,
        workspace_id: UUID,
        agent_id: UUID | None = None,
    ) -> None:
        self._factory = session_factory
        self._session_id = session_id
        self._workspace_id = workspace_id
        self._agent_id = agent_id
        self._buffer: list[str] = []
        self._pending: list[asyncio.Task[Any]] = []
        # Terminal frame deferred to after the turn's DB commit, so a client
        # refetch triggered by the frame sees the persisted rows.
        self._terminal_event: str | None = None
        self._error_message: str | None = None

    # ------------------------------------------------------------------
    # progress_reporter protocol (synchronous, called from the event loop)
    # ------------------------------------------------------------------

    def on_text(self, text: str) -> None:
        self._accumulate(text)
        self._publish("text", text=text)

    def on_text_delta(self, text: str) -> None:
        self._accumulate(text or "")
        self._publish("text_delta", text=text or "")

    def on_tool_call(self, tool_name: str, call_id: str) -> None:
        """Streamed only — tool activity is not persisted (§5.2.3)."""
        self._publish("tool_call", tool_name=tool_name, call_id=call_id)

    def on_tool_result(self, call_id: str) -> None:
        """Streamed only — see :meth:`on_tool_call`."""
        self._publish("tool_result", call_id=call_id)

    def on_turn_complete(self, event: Any, session: Any) -> None:
        self._terminal_event = "turn_complete"
        self._schedule(self._flush_turn())

    def on_session_complete(self, event: Any, session: Any) -> None:
        self._terminal_event = "session_complete"
        self._schedule(self._flush_turn())

    def on_error(self, message: str) -> None:
        # Flush whatever partial answer arrived so failures still render.
        self._terminal_event = "error"
        self._error_message = message
        self._schedule(self._flush_turn())

    # ------------------------------------------------------------------
    # Folding + persistence
    # ------------------------------------------------------------------

    def _accumulate(self, text: str) -> None:
        if text:
            self._buffer.append(text)

    def _schedule(self, coro: Any) -> None:
        try:
            self._pending.append(asyncio.create_task(coro))
        except Exception:
            logger.debug("chat bridge schedule failed", exc_info=True)

    def _publish(self, event: str, **payload: Any) -> None:
        """Fire-and-forget frame on ``chat.{session_id}`` (§6.1).

        The realtime channel is a notification surface, never a dependency
        of execution: broker absence or failure is logged at debug level.
        """
        try:
            asyncio.create_task(
                get_broker().publish(
                    f"chat.{self._session_id}",
                    {
                        "event": event,
                        "session_id": str(self._session_id),
                        **payload,
                    },
                )
            )
        except Exception:
            logger.debug("chat bridge publish failed", exc_info=True)

    async def _flush_turn(self) -> None:
        content = "".join(self._buffer)
        self._buffer.clear()
        if content:
            try:
                async with self._factory() as db:
                    repos = Repositories(db)
                    await repos.messages.append(
                        Message(
                            id=uuid.uuid4(),
                            session_id=self._session_id,
                            workspace_id=self._workspace_id,
                            seq=0,  # sentinel — repository auto-assigns
                            role="assistant",
                            content=content,
                            agent_id=self._agent_id,
                            author_label=None,
                            created_at=datetime.now(UTC),
                        )
                    )
                    await db.commit()
            except Exception:
                logger.error(
                    "chat bridge flush failed for session %s",
                    self._session_id,
                    exc_info=True,
                )
        terminal, self._terminal_event = self._terminal_event, None
        if terminal:
            frame: dict[str, Any] = {}
            if terminal == "error":
                frame["message"] = self._error_message
            self._publish(terminal, **frame)

    async def flush(self) -> None:
        """Await all scheduled DB writes (called by the runner on exit)."""
        # Close any turn the runner ended without a completion callback.
        if self._buffer:
            self._schedule(self._flush_turn())
        if not self._pending:
            return
        outcomes = await asyncio.gather(*self._pending, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.debug("chat bridge flush task failed", exc_info=outcome)


def is_progress_reporter(obj: Any) -> bool:
    """Duck-type guard: does *obj* look like a progress reporter?

    Mirrors the ``hasattr`` checks in the runner's event loop; kept here so
    the dispatcher (§6.1d) and tests share one definition.
    """
    return all(
        callable(getattr(obj, name, None))
        for name in ("on_text", "on_text_delta", "flush")
    )


__all__ = ["ChatMessageBridge", "is_progress_reporter"]
