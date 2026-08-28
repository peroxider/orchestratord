"""DshSession — wraps deepseek-harness-sdk Session into the AgentSession SPI."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.session import ResumeStatus
from orchestratord.spi.session import ResumeStatus


class DshSession:
    """Adapts a DeepSeek Harness SDK session into an AgentSession.

    The SDK is synchronous (blocking ``subscription.next()``), so all
    harness calls are dispatched via ``asyncio.to_thread`` to avoid
    blocking the event loop.
    """

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"dsh-{id(self)}"
        # cost_reporting=True is optimistic — the SDK may not surface a
        # `usage` field on every release.  ``_probe_cost_support`` will
        # downgrade this once the harness is instantiated and we know.
        self.capabilities = BackendCapabilities(
            streaming_deltas=False,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=False,
            takeover=False,
        )
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        self._harness: Any = None
        self._cost_probed = False

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    def _get_harness(self) -> Any:
        if self._harness is None:
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
            self._harness = DeepSeekHarness(config)
            self._harness.start()
            self._probe_cost_support()
        return self._harness

    def _probe_cost_support(self) -> None:
        """Downgrade ``cost_reporting`` if the linked SDK exposes no usage field.

        The optimistic default assumes the SDK ships ``usage`` on
        ``RunResult``. If the attribute is missing we re-build
        ``self.capabilities`` with the bit cleared so the orchestrator
        core can fall back to its token estimator instead of expecting
        data that will never arrive.
        """
        if self._cost_probed:
            return
        self._cost_probed = True
        sample = getattr(self._harness, "sample_run", None)
        has_usage_attr = sample is not None and "usage" in getattr(
            sample, "__dict__", {}
        )
        # Most SDK builds do not yet expose ``sample_run``; treat the
        # optimistic default as authoritative in that case.
        if sample is not None and not has_usage_attr:
            self.capabilities = BackendCapabilities(
                streaming_deltas=self.capabilities.streaming_deltas,
                resumable=self.capabilities.resumable,
                interrupt=self.capabilities.interrupt,
                approval_hooks=self.capabilities.approval_hooks,
                parallel_sessions=self.capabilities.parallel_sessions,
                cost_reporting=False,
                tool_filtering=self.capabilities.tool_filtering,
                takeover=self.capabilities.takeover,
            )

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        text = content if isinstance(content, str) else str(content)
        error_emitted = False
        try:
            try:
                harness = self._get_harness()
            except Exception as exc:
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.ERROR,
                        payload={
                            "code": "dsh_init_error",
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
                error_emitted = True
                return

            try:
                result = await asyncio.to_thread(
                    self._send_sync, text, harness
                )
            except Exception as exc:
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.ERROR,
                        payload={
                            "code": "dsh_error",
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
                error_emitted = True
            else:
                self._ingest_result(result)
                # If the SDK returned an abnormal finish, ``_ingest_result``
                # appended a synthetic ERROR event — propagate that signal
                # into the terminal SESSION_COMPLETE.
                if (
                    self._events
                    and self._events[-1].kind == EventKind.ERROR
                ):
                    error_emitted = True
        finally:
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.SESSION_COMPLETE,
                    payload={
                        "reason": "error" if error_emitted else "success",
                    },
                )
            )

    def _send_sync(self, text: str, harness: Any) -> Any:
        return harness.run(text, session_id=self.session_id)

    def _ingest_result(self, result: Any) -> None:
        """Translate DSH RunResult events into EventEnvelope stream."""
        for event in result.events:
            translated = self._translate_event(event)
            if translated is not None:
                self._events.append(translated)

        # When the SDK reports an abnormal finish and no ERROR event has
        # already been emitted by ``_translate_event`` (which is the
        # case today), surface it as one. ``SESSION_COMPLETE`` is
        # always emitted by ``send()``'s finally, not here — this
        # avoids the historical double-emit bug.
        if result.finish_reason not in (None, "success", "stop", "completed"):
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={
                        "code": "dsh_finish",
                        "reason": result.finish_reason,
                    },
                )
            )

    def _translate_event(self, event: dict[str, Any]) -> EventEnvelope | None:
        event_type = event.get("type", "")
        data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}

        if event_type == "assistant/message":
            message = data.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TEXT,
                payload={"text": "".join(text_parts)},
            )

        elif event_type == "tool/call":
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TOOL_CALL,
                payload={
                    "call_id": data.get("callId", ""),
                    "name": data.get("name", ""),
                    "arguments": data.get("arguments", {}),
                },
            )

        elif event_type == "tool/result":
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TOOL_RESULT,
                payload={
                    "call_id": data.get("callId", ""),
                    "ok": True,
                    "output": data.get("result"),
                },
            )

        elif event_type == "turn/end":
            reason_data = data.get("reason", {})
            reason_kind = reason_data.get("kind", "unknown") if isinstance(reason_data, dict) else str(reason_data)
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TURN_COMPLETE,
                payload={"reason": reason_kind},
            )

        return None

    async def _emit_events(self):
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    async def interrupt(self) -> None:
        pass

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        pass

    async def probe_resume(self) -> ResumeStatus:
        """DSH SDK does not expose a resume probe — always UNDETECTABLE.

        The orchestrator should still attempt ``send()``; if the SDK
        cannot find the resume target it will surface as an ERROR
        event on the normal stream.
        """
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        if self._harness is not None:
            await asyncio.to_thread(self._harness.close)
        self._closed = True

    def close_sync(self) -> None:
        if self._harness is not None:
            try:
                self._harness.close()
            except Exception:
                pass
        self._closed = True
