"""Chat dispatcher: claims pending sessions and drives the BackendRunner (§6.1d).

Session-creating entry points — chat start (§6.1c ``POST
/api/workspaces/{id}/chat/sessions``) and issue mention (§6.2) — deliberately
stop at ``status="pending"``. This loop is the other half: it atomically
claims one pending session at a time (``FOR UPDATE SKIP LOCKED`` so
concurrent dispatchers never double-claim), flips it to ``running``, invokes
the BackendRunner with a :class:`~orchestratord.chat_bridge.ChatMessageBridge`
attached as ``progress_reporter`` (folding assistant turns onto the chat
timeline), and flips the row to a terminal status on completion.

The actual runner invocation is behind the ``runner_invoke`` callable seam so
the dispatcher stays testable without agent configuration; the daemon wires
in the real ``BackendRunner``. A crash between claim and dispatch leaves a
``running`` row — stoppable via the §5.2.3 session-control endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestratord.chat_bridge import ChatMessageBridge
from orchestratord.db.models import Message
from orchestratord.db.models import Session as SessionRow
from orchestratord.db.repository import Repositories

logger = logging.getLogger(__name__)

RunnerInvoke = Callable[[SessionRow, str, ChatMessageBridge], Awaitable[None]]


def initial_message_of(messages: list[Message]) -> Message | None:
    """First ``role="user"`` message (lowest seq) of a chat timeline."""
    for message in messages:
        if message.role == "user":
            return message
    return None


class ChatDispatcher:
    """Claim-loop driver for pending chat/mention sessions (§6.1d)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runner_invoke: RunnerInvoke,
        interval: float = 5.0,
    ) -> None:
        self._factory = session_factory
        self._runner_invoke = runner_invoke
        self._interval = interval
        self._running = False
        # Set by wake() so an externally-created pending session (e.g. a
        # peer auto-scheduled turn, §6.1d) skips the poll interval.
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------

    async def claim_next(self) -> SessionRow | None:
        """Atomically claim the oldest pending session, flipping it running.

        ``FOR UPDATE SKIP LOCKED`` keeps concurrent dispatchers from
        double-claiming; the status flip commits with the row lock so a crash
        right after leaves a ``running`` row the §5.2.3 control endpoints can
        stop.
        """
        async with self._factory() as db:
            row = (
                await db.execute(
                    select(SessionRow)
                    .where(SessionRow.status == "pending")
                    .order_by(SessionRow.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "running"
            await db.commit()
            return row

    async def pending_count(self) -> int:
        async with self._factory() as db:
            rows = await db.execute(
                select(SessionRow).where(SessionRow.status == "pending")
            )
            return len(rows.scalars().all())

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _initial_prompt(self, session_id: uuid.UUID) -> str:
        async with self._factory() as db:
            messages = await Repositories(db).messages.list_for_session(session_id)
            first = initial_message_of(messages)
            return first.content if first is not None else ""

    async def dispatch_one(self, session: SessionRow) -> str:
        """Run one claimed session to a terminal status; return that status."""
        bridge = ChatMessageBridge(
            self._factory, session.id, session.workspace_id, agent_id=session.agent_id
        )
        prompt = await self._initial_prompt(session.id)
        terminal = "completed"
        try:
            await self._runner_invoke(session, prompt, bridge)
        except Exception:
            terminal = "failed"
            logger.exception("chat dispatch failed for session %s", session.id)
        finally:
            await bridge.flush()
        await self._set_terminal(session.id, terminal)
        return terminal

    async def _set_terminal(self, session_id: uuid.UUID, status: str) -> None:
        async with self._factory() as db:
            row = (
                await db.execute(
                    select(SessionRow)
                    .where(SessionRow.id == session_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "chat dispatch terminal write missed session %s", session_id
                )
                return
            # A §5.2.3 stop/kill may have already moved it off ``running``;
            # never resurrect a terminal or externally-updated row.
            if row.status == "running":
                row.status = status
                await db.commit()

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        self._running = True
        while self._running:
            try:
                claimed = await self.claim_next()
            except Exception:
                logger.exception("chat dispatcher claim failed")
                claimed = None
            if claimed is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._interval
                    )
                except TimeoutError:
                    pass
                continue
            try:
                await self.dispatch_one(claimed)
            except Exception:
                # dispatch_one already funnels failures into ``failed``;
                # this guard keeps the loop alive against surprises.
                logger.exception("chat dispatcher dispatch failed")
                await self._set_terminal(claimed.id, "failed")

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def wake(self) -> None:
        """Interrupt the idle wait so a fresh pending session is claimed now."""
        self._wake.set()
