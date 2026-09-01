"""DshSession — wraps deepseek-harness-sdk Session into the AgentSession SPI.

The SDK is synchronous (blocking ``subscription.next()`` inside
``Session.run``), so each turn is dispatched to a worker thread while a
notification pump forwards ``session.event`` payloads into an
``asyncio.Queue``.  ``send()`` returns as soon as the turn is
dispatched; events flow incrementally through ``events()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

logger = logging.getLogger(__name__)


class DshSession:
    """Adapts a DeepSeek Harness SDK session into an AgentSession.

    Each ``send()`` dispatches one blocking ``harness.run()`` turn to a
    worker thread. The SDK invokes ``on_notification`` on that thread
    for every notification as it arrives; translated envelopes are
    handed back to the event loop via ``call_soon_threadsafe`` and
    drained by ``events()``. A ``SESSION_COMPLETE`` envelope is always
    the last event of a turn, even when the turn fails.
    """

    def __init__(
        self,
        spec: SessionSpec,
        *,
        harness_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._spec = spec
        # ``id(self)`` looked unique but CPython reuses object ids
        # after GC — two sessions could collide with a live persisted
        # one. A random uuid cannot.
        self.session_id = spec.resume_session_id or f"dsh-{uuid.uuid4().hex[:12]}"
        # streaming_deltas=True: the notification pump forwards
        # assistant/chunk text-delta / reasoning-delta frames as they
        # arrive, so the core consumes real deltas (no pseudo-split).
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            # Honesty: cross-process resume hits the runtime's
            # "id collision" guard (no remount protocol for persisted
            # sessions). Same-process multi-turn works but the bit
            # promises more than that.
            resumable=False,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=False,
            takeover=False,
        )
        self._harness_factory = harness_factory
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._turn_started = False
        self._seq = 0
        self._closed = False
        self._harness: Any = None
        self._last_error_message: str | None = None
        # Real token usage accumulated from assistant/message
        # events (data.usage) — surfaced on SESSION_COMPLETE.
        self._usage_totals: dict[str, int] = {}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    def _default_harness_factory(self) -> Any:
        from deepseek_harness.api import DeepSeekHarness, DeepSeekHarnessConfig

        config = DeepSeekHarnessConfig(
            cwd=self._spec.cwd,
            model=self._spec.model or "deepseek-v4-flash",
            provider=self._spec.provider or "deepseek-official",
            env=self._spec.env,
            base_url=self._spec.base_url,
            api_key=self._spec.api_key,
            cordis=self._spec.cordis,
            runtime_bin=self._spec.runtime_bin,
        )
        harness = DeepSeekHarness(config)
        harness.start()
        return harness

    def _ensure_harness(self) -> Any:
        if self._harness is None:
            factory = self._harness_factory or self._default_harness_factory
            self._harness = factory()
        return self._harness

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        text = content if isinstance(content, str) else str(content)

        # Serialize turns: a follow-up send waits for the previous turn
        # thread to finish (including its terminal SESSION_COMPLETE).
        if self._turn_task is not None:
            await self._turn_task

        self._loop = asyncio.get_running_loop()
        self._turn_started = True
        self._turn_task = asyncio.create_task(
            asyncio.to_thread(self._run_turn, text)
        )

    def _run_turn(self, text: str) -> None:
        """Blocking turn body — runs on a worker thread."""
        error_emitted = False
        try:
            try:
                if self._closed:
                    raise RuntimeError("session closed before turn started")
                harness = self._ensure_harness()
            except Exception as exc:  # noqa: BLE001 - SPI boundary: any SDK failure must surface as an ERROR event
                error_message = f"{type(exc).__name__}: {exc}"
                self._last_error_message = error_message
                self._emit_threadsafe(
                    EventKind.ERROR,
                    {
                        "code": "dsh_init_error",
                        "message": error_message,
                    },
                )
                error_emitted = True
                return

            try:
                result = harness.run(
                    text,
                    session_id=self.session_id,
                    on_notification=self._on_notification,
                )
            except Exception as exc:  # noqa: BLE001 - SPI boundary: any SDK failure must surface as an ERROR event
                error_message = f"{type(exc).__name__}: {exc}"
                self._last_error_message = error_message
                self._emit_threadsafe(
                    EventKind.ERROR,
                    {
                        "code": "dsh_error",
                        "message": error_message,
                    },
                )
                error_emitted = True
            else:
                # Incremental events were forwarded by the notification
                # pump; the batched ``result.events`` are intentionally
                # not re-emitted. Only the finish reason is read here.
                if result.finish_reason not in (
                    None,
                    "success",
                    "stop",
                    "completed",
                ):
                    # The core reads payload["message"] — without
                    # it the failure surfaces as "unknown error".
                    self._emit_threadsafe(
                        EventKind.ERROR,
                        {
                            "code": "dsh_finish",
                            "reason": result.finish_reason,
                            "message": self._last_error_message
                            or f"turn finished with reason={result.finish_reason}",
                        },
                    )
                    error_emitted = True
        finally:
            complete_payload: dict[str, Any] = {
                "reason": "error" if error_emitted else "success"
            }
            if self._usage_totals:
                # Real token usage (not fabricated USD — DeepSeek
                # prices are not invented here; the core/consumer can
                # convert with its own pricing table).
                complete_payload["usage"] = dict(self._usage_totals)
            self._emit_threadsafe(EventKind.SESSION_COMPLETE, complete_payload)

    def _on_notification(self, notification: Any) -> None:
        """SDK callback — invoked on the worker thread per notification."""
        if getattr(notification, "method", None) != "session.event":
            return
        payload = getattr(notification, "payload", None)
        if not isinstance(payload, dict):
            return
        if payload.get("sessionId") != self.session_id:
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        for translated in self._translate_event(event):
            self._enqueue_threadsafe(translated)

    def _emit_threadsafe(self, kind: EventKind, payload: dict[str, Any]) -> None:
        envelope = EventEnvelope(
            seq=self._next_seq(),
            timestamp=self._now(),
            kind=kind,
            payload=payload,
        )
        self._enqueue_threadsafe(envelope)

    def _enqueue_threadsafe(self, envelope: EventEnvelope) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._queue.put_nowait, envelope)
        except RuntimeError:
            # Event loop closed mid-turn (e.g. close() during an active
            # turn) — the consumer is gone; drop the event.
            logger.debug("dsh event dropped after loop close: %s", envelope.kind)

    def _translate_event(
        self, event: dict[str, Any]
    ) -> list[EventEnvelope]:
        """Translate one DSH wire event into 0..n SPI envelopes."""
        event_type = event.get("type", "")
        data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}

        if event_type == "assistant/message":
            message = data.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            # Accumulate real token usage (data.usage) so the
            # SESSION_COMPLETE payload can carry session totals.
            usage = data.get("usage")
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if isinstance(value, (int, float)):
                        self._usage_totals[key] = (
                            self._usage_totals.get(key, 0) + int(value)
                        )
            return [
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TEXT,
                    payload={"text": "".join(text_parts)},
                )
            ]

        elif event_type == "assistant/chunk":
            chunk = data.get("chunk", {}) if isinstance(data.get("chunk"), dict) else {}
            chunk_type = chunk.get("type", "")
            if chunk_type in ("text-delta", "reasoning-delta"):
                return [
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.TEXT_DELTA,
                        payload={"text": str(chunk.get("text", ""))},
                    )
                ]
            return []

        elif event_type == "tool/call":
            return [
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TOOL_CALL,
                    payload={
                        "call_id": data.get("callId", ""),
                        "name": data.get("name", ""),
                        "arguments": data.get("arguments", {}),
                    },
                )
            ]

        elif event_type == "tool/result":
            # Real wire shape:
            #   data.message.content[0].toolCallId  (fallback:
            #   data.message.source.callId), text under
            #   content[*].content[*].text, error flag `isError`.
            message = data.get("message", {})
            if not isinstance(message, dict):
                message = {}
            blocks = message.get("content", [])
            if not isinstance(blocks, list):
                blocks = []
            call_id = ""
            texts: list[str] = []
            is_error = False
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if not call_id and block.get("toolCallId"):
                    call_id = str(block["toolCallId"])
                if block.get("isError"):
                    is_error = True
                inner = block.get("content", [])
                if isinstance(inner, list):
                    for part in inner:
                        if isinstance(part, dict) and part.get("text"):
                            texts.append(str(part["text"]))
            if not call_id:
                source = message.get("source", {})
                if isinstance(source, dict) and source.get("callId"):
                    call_id = str(source["callId"])
            return [
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TOOL_RESULT,
                    payload={
                        "call_id": call_id,
                        "ok": not is_error,
                        "output": "\n".join(texts),
                    },
                )
            ]

        elif event_type == "turn/end":
            reason_data = data.get("reason", {})
            reason_kind = reason_data.get("kind", "unknown") if isinstance(reason_data, dict) else str(reason_data)
            envelopes = [
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TURN_COMPLETE,
                    payload={"reason": reason_kind},
                )
            ]
            if reason_kind == "error":
                # Surface data.reason.error.{message,code} — the
                # core reads payload["message"] and the historical
                # stream only showed a bare reason=error.
                error_info = (
                    reason_data.get("error", {})
                    if isinstance(reason_data, dict)
                    else {}
                )
                if not isinstance(error_info, dict):
                    error_info = {}
                message_text = str(
                    error_info.get("message")
                    or f"turn ended with reason={reason_kind}"
                )
                code = str(error_info.get("code") or "dsh_turn_error")
                self._last_error_message = message_text
                envelopes.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.ERROR,
                        payload={"code": code, "message": message_text},
                    )
                )
            return envelopes

        return []

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[EventEnvelope]:
        if not self._turn_started:
            # Historical contract: events() before any send() is an
            # exhausted stream, not a hanging one.
            return
        while True:
            envelope = await self._queue.get()
            yield envelope
            if envelope.kind is EventKind.SESSION_COMPLETE:
                break

    async def interrupt(self) -> None:
        pass

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        pass

    async def probe_resume(self) -> ResumeStatus:
        """DSH SDK exposes no resume probe — detection is unavailable.

        With a resume target configured the result is UNDETECTABLE (the
        orchestrator still attempts ``send()``; a missing resume target
        surfaces as an ERROR event on the normal stream). Without one
        the session is trivially "fresh", reported as RESUMED.
        """
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            # The SDK has no session cancel, so a turn
            # in flight cannot be terminated — closing the harness makes
            # the worker fail on its next transport read. Log it so an
            # unexpected post-close worker burst is diagnosable.
            logger.warning(
                "dsh session closed while a turn was still in flight "
                "(session_id=%s) — the worker will unwind on its own",
                self.session_id,
            )
        self._closed = True
        if self._harness is not None:
            try:
                await asyncio.to_thread(self._harness.close)
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("dsh harness close failed: %s", exc)
            self._harness = None

    def close_sync(self) -> None:
        self._closed = True
        if self._harness is not None:
            try:
                self._harness.close()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.debug("dsh harness close failed: %s", exc)
            self._harness = None
