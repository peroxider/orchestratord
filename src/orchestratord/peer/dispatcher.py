"""Inbound peer message dispatcher (DESIGN §6.1; D18, D19, NG4, R12).

Sits behind ``POST /api/peer/peers/{orch-id}/invoke`` and turns the
at-least-once wire semantics into exactly the three behaviors the
design pins:

* **D18 dedup** — results are cached per ``(orch_id, msg_id)`` inside
  a sliding window (``peer.dedup_window_seconds``, default 30s); a
  re-delivered INVOKE is answered from the cache instead of
  re-executing the underlying operation.
* **D19 ordering** — INVOKEs may carry a per-session monotonic
  ``ordering``; a gap logs a warning and flags the dispatch as
  ``out_of_order`` (for the audit row) but never blocks delivery.
  Cross-session ordering is undefined.
* **NG4 auto-schedule gate** — a persisted remote message only
  schedules an agent turn when ``peer.auto_schedule`` is true, and
  even then under at most ``max_concurrent_peer_turns`` live turns
  (R12); when the ceiling is reached the message stays persisted and
  the dispatch is reported unscheduled.

The dispatcher owns no persistence itself: the caller passes an
``execute`` coroutine that performs the operation and returns the
RESULT body, keeping the dispatcher unit-testable without a database.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from orchestratord.config.schema import PeerConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchOutcome:
    """Result metadata for one inbound INVOKE."""

    duplicate: bool  # answered from the D18 dedup window
    out_of_order: bool  # D19 gap detected (warn + audit flag)
    scheduled: bool  # auto-scheduled into an agent turn (NG4/R12)


# The daemon-level hook that turns a persisted remote message into an
# agent turn (wired to the chat dispatcher in PR6; tests inject fakes).
TurnScheduler = Callable[[str, dict[str, Any]], Awaitable[None]]


class PeerMessageDispatcher:
    """Dedup / ordering / auto-schedule brain for inbound peer INVOKEs."""

    def __init__(
        self,
        *,
        config: PeerConfig | None = None,
        turn_scheduler: TurnScheduler | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or PeerConfig.from_env()
        self._turn_scheduler = turn_scheduler
        self._clock = clock
        # (orch_id, msg_id) -> (result_body, expires_at)
        self._results: dict[tuple[str, str], tuple[Any, float]] = {}
        # (orch_id, session_id) -> last accepted ordering seq
        self._ordering: dict[tuple[str, str], int] = {}
        # R12: live scheduled-turn count, capped by max_concurrent_peer_turns
        self._active_turns = 0
        self._live_turns: set[asyncio.Task[None]] = set()

    # -- D18: dedup window --

    def cached_result(self, orch_id: str, msg_id: str) -> Any | None:
        """The cached RESULT body for a re-delivered msg_id, if fresh."""
        key = (orch_id, msg_id)
        hit = self._results.get(key)
        if hit is None:
            return None
        result, expires_at = hit
        if expires_at <= self._clock():
            self._results.pop(key, None)
            return None
        return result

    def remember_result(self, orch_id: str, msg_id: str, result: Any) -> None:
        ttl = self._config.dedup_window_seconds
        self._results[(orch_id, msg_id)] = (result, self._clock() + ttl)
        self._prune()

    def _prune(self) -> None:
        now = self._clock()
        for key in [
            key
            for key, (_, expires_at) in self._results.items()
            if expires_at <= now
        ]:
            self._results.pop(key, None)

    # -- D19: per-session ordering --

    def check_ordering(
        self, orch_id: str, session_id: str, ordering: str | int | None
    ) -> bool:
        """True when the seq continues the per-session chain (D19).

        ``None`` (unordered INVOKE) is always in order. A gap or repeat
        warns and returns False — the message is still delivered; the
        caller marks the audit row ``out_of_order``.
        """
        if ordering is None:
            return True
        try:
            seq = int(ordering)
        except (TypeError, ValueError):
            logger.warning(
                "peer %s sent non-integer ordering %r for session %s",
                orch_id,
                ordering,
                session_id,
            )
            return False
        key = (orch_id, str(session_id))
        last = self._ordering.get(key)
        if last is not None and seq != last + 1:
            logger.warning(
                "D19 ordering gap from %s on session %s: expected %d, got %d",
                orch_id,
                session_id,
                last + 1,
                seq,
            )
            self._ordering[key] = max(seq, last)
            return False
        self._ordering[key] = seq
        return True

    # -- dispatch --

    async def dispatch_message(
        self,
        *,
        orch_id: str,
        msg_id: str,
        session_id: str | None = None,
        ordering: str | int | None = None,
        payload: dict[str, Any] | None = None,
        execute: Callable[[], Awaitable[Any]],
    ) -> DispatchOutcome:
        """Run dedup → order-check → execute → optional auto-schedule.

        On a dedup hit ``execute`` is not called at all; the caller
        answers with :meth:`cached_result`'s body.
        """
        if self.cached_result(orch_id, msg_id) is not None:
            return DispatchOutcome(
                duplicate=True, out_of_order=False, scheduled=False
            )
        out_of_order = session_id is not None and not self.check_ordering(
            orch_id, session_id, ordering
        )
        result = await execute()
        self.remember_result(orch_id, msg_id, result)
        scheduled = False
        if self._config.auto_schedule and self._turn_scheduler is not None:
            scheduled = await self._schedule_turn(session_id or "", payload or {})
        return DispatchOutcome(
            duplicate=False, out_of_order=out_of_order, scheduled=scheduled
        )

    # -- NG4/R12: auto-schedule under a turn ceiling --

    def set_auto_schedule(self, enabled: bool) -> None:
        """Operator toggle for the NG4 gate (the auto-schedule 开关)."""
        self._config.auto_schedule = enabled

    @property
    def auto_schedule(self) -> bool:
        return self._config.auto_schedule

    async def _schedule_turn(
        self, session_id: str, payload: dict[str, Any]
    ) -> bool:
        if self._active_turns >= max(1, self._config.max_concurrent_peer_turns):
            logger.warning(
                "R12: peer turn ceiling (%d) reached; message on session "
                "%s persisted but not scheduled",
                self._config.max_concurrent_peer_turns,
                session_id,
            )
            return False
        self._active_turns += 1

        def _release(task: asyncio.Task[None]) -> None:
            self._active_turns -= 1
            self._live_turns.discard(task)

        task = asyncio.create_task(
            self._run_turn(session_id, payload), name="peer-auto-turn"
        )
        task.add_done_callback(_release)
        self._live_turns.add(task)
        return True

    async def _run_turn(self, session_id: str, payload: dict[str, Any]) -> None:
        try:
            await self._turn_scheduler(session_id, payload)
        except Exception:  # a failed turn must not kill dispatch
            logger.exception(
                "peer auto-scheduled turn failed on session %s", session_id
            )

    async def wait_for_turns(self) -> None:
        """Block until all in-flight auto-scheduled turns finish (tests)."""
        if self._live_turns:
            await asyncio.gather(*list(self._live_turns))
