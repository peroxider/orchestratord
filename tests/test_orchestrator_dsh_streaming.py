"""streaming pump tests for the dsh backend.

The dsh adapter historically blocked ``send()`` until the harness turn
completed and then dumped every event at once ("batch events"), which
made the core blind for the whole turn (no tool counts, no tail, no
control-plane drain, dead five-level timeouts).

These tests pin the pump contract at the SPI seam (``send``/``events``/
capabilities) using a fake harness that mirrors the real
``deepseek_harness.api`` interface:

* ``DeepSeekHarness.start()``
* ``DeepSeekHarness.run(input, *, session_id, on_notification)`` —
  calls ``on_notification(Notification)`` incrementally as the turn
  progresses, returns a ``RunResult`` at the end.
* ``DeepSeekHarness.close()``
"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from typing import Any

from orchestratord_dsh.backend import DshBackend
from orchestratord_dsh.session import DshSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind


def _notification(session_id: str, event: dict) -> SimpleNamespace:
    """Build a Notification-shaped object without importing the SDK."""
    return SimpleNamespace(
        method="session.event",
        payload={"sessionId": session_id, "event": event},
    )


class FakeHarness:
    """Mimics deepseek_harness.api.DeepSeekHarness with a scripted turn.

    Each scripted step is ``(delay_seconds, event_dict)``. Steps are
    replayed with real sleeps so the test can observe incremental
    delivery while ``run()`` is still in flight.
    """

    def __init__(self, script: list[tuple[float, dict]], tail_sleep: float = 0.0) -> None:
        self._script = script
        self._tail_sleep = tail_sleep
        self.started = False
        self.closed = False
        self.run_returned_at: float | None = None

    def start(self) -> None:
        self.started = True

    def run(self, input, *, session_id=None, on_notification=None):
        events: list[dict] = []
        for delay, event in self._script:
            if delay:
                time.sleep(delay)
            events.append(event)
            if on_notification is not None:
                on_notification(_notification(session_id, event))
        if self._tail_sleep:
            time.sleep(self._tail_sleep)
        self.run_returned_at = time.monotonic()
        return SimpleNamespace(
            session_id=session_id,
            final_response="done",
            finish_reason="completed",
            events=events,
            notifications=[],
            session_root=None,
        )

    def close(self) -> None:
        self.closed = True


def _spec() -> SessionSpec:
    return SessionSpec(cwd="/tmp", model="deepseek-v4-flash", provider="deepseek-official")


def _script() -> list[tuple[float, dict]]:
    return [
        (0.0, {"type": "assistant/chunk", "data": {"chunk": {"type": "text-delta", "text": "Hel"}}}),
        (0.2, {"type": "assistant/chunk", "data": {"chunk": {"type": "text-delta", "text": "lo"}}}),
        (0.2, {"type": "assistant/message", "data": {"message": {"content": [{"type": "text", "text": "Hello"}]}}}),
        (0.0, {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}),
    ]


async def _drain(session: DshSession, timeout: float = 5.0) -> list:
    """Consume the session stream until SESSION_COMPLETE (inclusive)."""
    agen = session.events()
    out = []
    try:
        while True:
            ev = await asyncio.wait_for(anext(agen), timeout=timeout)
            out.append(ev)
            if ev.kind is EventKind.SESSION_COMPLETE:
                break
    finally:
        await agen.aclose()
    return out


class TestDshStreamingPump(unittest.IsolatedAsyncioTestCase):
    async def test_send_returns_before_turn_completes(self) -> None:
        """send() must not block until the harness turn finishes."""
        # Turn lasts ≥1.0s of real sleeps after dispatch.
        harness = FakeHarness(_script(), tail_sleep=1.0)
        session = DshSession(_spec(), harness_factory=lambda: harness)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.wait_for(session.send("task"), timeout=0.5)
        elapsed = loop.time() - t0
        self.assertLess(
            elapsed,
            0.5,
            "send() blocked on the harness turn instead of returning immediately",
        )
        # The turn is still running; its events must still be consumable.
        events = await _drain(session)
        self.assertEqual(events[-1].kind, EventKind.SESSION_COMPLETE)
        self.assertIsNotNone(harness.run_returned_at)
        await session.close()

    async def test_events_arrive_incrementally_while_turn_in_flight(self) -> None:
        """First TEXT_DELTA must be consumable before the turn ends."""
        harness = FakeHarness(_script(), tail_sleep=0.5)
        session = DshSession(_spec(), harness_factory=lambda: harness)

        await session.send("task")
        agen = session.events()
        try:
            first = await asyncio.wait_for(anext(agen), timeout=1.0)
        finally:
            await agen.aclose()
        self.assertIs(first.kind, EventKind.TEXT_DELTA)
        self.assertIsNone(
            harness.run_returned_at,
            "turn must still be in flight when the first delta arrives",
        )
        rest = await _drain(session)
        self.assertEqual(rest[-1].kind, EventKind.SESSION_COMPLETE)
        await session.close()

    async def test_stream_yields_full_pipeline_and_terminal(self) -> None:
        """Deltas are delivered once before turn and session completion."""
        harness = FakeHarness(_script())
        session = DshSession(_spec(), harness_factory=lambda: harness)

        await session.send("task")
        kinds = [ev.kind async for ev in session.events()]
        self.assertEqual(
            kinds,
            [
                EventKind.TEXT_DELTA,
                EventKind.TEXT_DELTA,
                EventKind.TURN_COMPLETE,
                EventKind.SESSION_COMPLETE,
            ],
        )
        await session.close()

    async def test_streaming_deltas_capability_is_true(self) -> None:
        """The pump makes real deltas available — the bit must be lit."""
        caps = DshBackend().capabilities()
        self.assertTrue(
            caps.streaming_deltas,
            "dsh streams assistant/chunk deltas via the notification pump; "
            "streaming_deltas=False forces the core to split TEXT into "
            "pseudo-deltas.",
        )
        session = DshSession(_spec())
        self.assertTrue(session.capabilities.streaming_deltas)

    async def test_error_in_turn_emits_error_then_single_session_complete(self) -> None:
        """An SDK exception mid-turn surfaces as ERROR + SESSION_COMPLETE."""

        class BoomHarness(FakeHarness):
            def run(self, input, *, session_id=None, on_notification=None):
                raise RuntimeError("runtime exploded")

        session = DshSession(_spec(), harness_factory=lambda: BoomHarness([]))
        await session.send("task")
        kinds = [ev.kind async for ev in session.events()]
        self.assertIn(EventKind.ERROR, kinds)
        self.assertEqual(kinds[-1], EventKind.SESSION_COMPLETE)
        self.assertEqual(kinds.count(EventKind.SESSION_COMPLETE), 1)

    async def test_sequential_sends_supported(self) -> None:
        """A follow-up send after SESSION_COMPLETE starts a new turn."""
        harness = FakeHarness(_script())
        session = DshSession(_spec(), harness_factory=lambda: harness)

        await session.send("first")
        first = [ev.kind async for ev in session.events()]
        self.assertEqual(first[-1], EventKind.SESSION_COMPLETE)

        await session.send("second")
        second = [ev.kind async for ev in session.events()]
        self.assertEqual(second[-1], EventKind.SESSION_COMPLETE)
        await session.close()

    async def test_events_before_any_send_terminates(self) -> None:
        """events() before any send() must not hang (historical contract)."""
        harness = FakeHarness(_script())
        session = DshSession(_spec(), harness_factory=lambda: harness)
        agen = session.events()
        try:
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(anext(agen), timeout=1.0)
        finally:
            await agen.aclose()
        await session.close()

    async def test_close_during_active_turn_does_not_hang(self) -> None:
        """close() while the turn runs must return promptly and stay safe."""
        harness = FakeHarness(_script(), tail_sleep=1.0)
        session = DshSession(_spec(), harness_factory=lambda: harness)
        await asyncio.wait_for(session.send("task"), timeout=0.5)
        t0 = time.monotonic()
        await asyncio.wait_for(session.close(), timeout=2.0)
        self.assertLess(time.monotonic() - t0, 2.0)

    async def test_streaming_output_text_is_not_duplicated(self) -> None:
        """Deltas + full message must not double-write output_text.

        The SDK streams assistant/chunk TEXT_DELTA frames first and then
        re-delivers the same text as a complete assistant/message TEXT
        event.  Both shapes land in ``session.output_text`` — without
        dedup the same turn renders as ``"HelloHello"`` instead of
        ``"Hello"``.  This pins the total count of text events reaching
        the progress sink as well (exactly the two deltas, no TEXT).
        """
        harness = FakeHarness(_script())
        session = DshSession(_spec(), harness_factory=lambda: harness)

        await session.send("task")
        output_text = ""
        sink = _TextSink()
        async for ev in session.events():
            if ev.kind is EventKind.TEXT:
                text = ev.payload.get("text", "")
                sink.on_text(text)
                output_text += text
            elif ev.kind is EventKind.TEXT_DELTA:
                text = ev.payload.get("text", "") or ev.payload.get("delta", "")
                sink.on_text_delta(text)
                output_text += text

        # Regression (#36): the complete message must not re-emit what
        # chunks already streamed.
        self.assertEqual(
            output_text,
            "Hello",
            f"output_text was double-written: {output_text!r}",
        )
        self.assertEqual(
            [kind for kind, _ in sink.text_events],
            ["text_delta", "text_delta"],
            "progress sink must see exactly the two TEXT_DELTA events, "
            "no duplicate TEXT",
        )
        await session.close()

    async def test_pure_assistant_message_without_chunks_emits_text(self) -> None:
        """A message that never streamed deltas must still emit TEXT.

        The dedup contract only skips the full TEXT for messages whose
        deltas were already forwarded; a pure ``assistant/message``
        (no preceding ``assistant/chunk``) is the only TEXT the core
        would ever see for that turn.
        """
        harness = FakeHarness(
            [
                (0.0, {"type": "assistant/message", "data": {"message": {"content": [{"type": "text", "text": "Hello"}]}}}),
                (0.0, {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}),
            ]
        )
        session = DshSession(_spec(), harness_factory=lambda: harness)

        await session.send("task")
        text_events = [
            (ev.kind, ev.payload.get("text", ""))
            async for ev in session.events()
            if ev.kind in (EventKind.TEXT, EventKind.TEXT_DELTA)
        ]
        self.assertEqual(
            text_events,
            [(EventKind.TEXT, "Hello")],
            "pure assistant/message must emit exactly one TEXT event",
        )
        await session.close()

    async def test_runner_consumes_streaming_events_without_double_write(self) -> None:
        """BackendRunner must fold the dsh stream into output_text once.

        Runner-level regression (#36): the real dsh session (streaming
        fixture) drives ``BackendRunner._process_events`` end to end —
        the core sees the same event stream it would in production and
        must land ``output_text == "Hello"`` (not "HelloHello") while
        the progress sink sees exactly two TEXT_DELTA pushes.
        """
        from orchestratord.backend_runner import BackendRunner

        runner = object.__new__(BackendRunner)
        runner._check_file_changes = lambda *_args: _changed()  # type: ignore[method-assign]
        sink = _TextSink()
        session = SimpleNamespace(
            turn_count=0,
            tool_count=0,
            status="running",
            output_text="",
            session_end_reason=None,
            session_end_summary=None,
            control_socket=None,
            created_at=time.time(),
            started_at=None,
            completed_at=None,
            duration_ms=None,
            paused=False,
            last_agent_event=None,
            last_tool_name=None,
            cost_usd=0.0,
            token_usage={},
            backend_error_detail=None,
            backend_session_id=None,
            run_id="run-test",
            workspace=SimpleNamespace(path="/tmp"),
            issue=SimpleNamespace(id="test-issue"),
        )

        # Real dsh session: the turn runs in a worker thread and events
        # flow through the notification pump into the runner's loop.
        harness = FakeHarness(_script())
        spi_session = DshSession(_spec(), harness_factory=lambda: harness)
        await spi_session.send("task")

        await runner._process_events(
            spi_session,
            session,
            {},
            None,
            None,
            None,
            sink,
        )

        self.assertEqual(
            session.output_text,
            "Hello",
            f"runner output_text was double-written: {session.output_text!r}",
        )
        self.assertEqual(
            [kind for kind, _ in sink.text_events],
            ["text_delta", "text_delta"],
            "progress sink must see exactly the two TEXT_DELTA events",
        )
        self.assertEqual(session.status, "completed")
        await spi_session.close()


class _TextSink:
    """Progress-sink double: records text events the runner pushes."""

    def __init__(self) -> None:
        self.text_events: list[tuple[str, str]] = []
        self.turn_events: list[tuple[Any, Any]] = []
        self.completions: list[Any] = []

    def on_text(self, text: str) -> None:
        self.text_events.append(("text", text))

    def on_text_delta(self, text: str) -> None:
        self.text_events.append(("text_delta", text))

    def on_turn_complete(self, event: Any, session: Any) -> None:
        self.turn_events.append((event, session))

    def on_session_complete(self, event: Any, session: Any) -> None:
        self.completions.append((event, session))


async def _changed() -> bool:
    return True


def test_session_id_fallback_is_uuid_backed() -> None:
    """The fallback session id must not derive from ``id(self)``
    (CPython reuses object ids after GC → collision with a live
    persisted session). It must be a unique dsh-prefixed uuid.
    """
    seen: set[str] = set()
    for _ in range(50):
        session = DshSession(_spec())
        sid = session.session_id
        assert sid.startswith("dsh-")
        suffix = sid[len("dsh-") :]
        assert len(suffix) == 12 and all(c in "0123456789abcdef" for c in suffix), (
            f"fallback id must be a 12-char uuid hex, got {sid!r}"
        )
        assert sid not in seen, "fallback session ids must be unique"
        seen.add(sid)
