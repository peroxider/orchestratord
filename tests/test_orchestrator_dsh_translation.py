"""Dsh event payloads must match the SDK.

The adapter historically read fields that do not exist on the real
payload (``data.callId`` / ``data.result`` for tool results), producing
``tool_result {'call_id': '', 'ok': True, 'output': None}`` and
destroying tool audit. These tests pin the translation against the
REAL shapes observed on the wire:

* ``tool/result`` → ``data.message.content[0].toolCallId``,
  ``data.message.content[*].content[*].text``, ``isError``
  (fallback: ``data.message.source.callId``)
* ``turn/end`` errors → ``data.reason.error.{message,code}`` must land
  in an ERROR event, and ``dsh_finish`` must carry ``message``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from orchestratord_dsh.session import DshSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind


def _notification(session_id: str, event: dict) -> SimpleNamespace:
    return SimpleNamespace(
        method="session.event",
        payload={"sessionId": session_id, "event": event},
    )


class _ScriptedHarness:
    """Feeds scripted events through the on_notification callback."""

    def __init__(self, script: list[dict], finish_reason: str | None = "completed") -> None:
        self._script = script
        self._finish_reason = finish_reason

    def start(self) -> None:
        return None

    def run(self, input, *, session_id=None, on_notification=None):
        events: list[dict] = []
        for event in self._script:
            events.append(event)
            if on_notification is not None:
                on_notification(_notification(session_id, event))
        return SimpleNamespace(
            session_id=session_id,
            final_response="",
            finish_reason=self._finish_reason,
            events=events,
            notifications=[],
            session_root=None,
        )

    def close(self) -> None:
        return None


def _session(script: list[dict], finish_reason: str | None = "completed") -> DshSession:
    return DshSession(
        SessionSpec(cwd="/tmp"),
        harness_factory=lambda: _ScriptedHarness(script, finish_reason=finish_reason),
    )


def _run(script: list[dict], finish_reason: str | None = "completed") -> list:
    session = _session(script, finish_reason=finish_reason)

    async def main() -> list:
        await session.send("task")
        out = []
        async for ev in session.events():
            out.append(ev)
            if ev.kind is EventKind.SESSION_COMPLETE:
                break
        await session.close()
        return out

    return asyncio.run(main())


def _tool_result_event(
    tool_call_id: str,
    text: str,
    *,
    is_error: bool = False,
    with_source_call_id: str | None = None,
) -> dict:
    message: dict = {
        "content": [
            {
                "type": "tool_result",
                "toolCallId": tool_call_id,
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            }
        ]
    }
    if with_source_call_id is not None:
        message["source"] = {"callId": with_source_call_id}
    return {"type": "tool/result", "data": {"message": message}}


class TestToolResultMapping:
    def test_real_payload_shape_maps_call_id_and_output(self) -> None:
        """The real wire shape (data.message.content[0]) must be read —
        not the phantom ``data.callId`` / ``data.result``.
        """
        events = _run(
            [
                _tool_result_event("call_abc", "file contents here"),
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
        )
        tool_results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
        assert len(tool_results) == 1
        payload = tool_results[0].payload
        assert payload["call_id"] == "call_abc"
        assert payload["ok"] is True
        assert payload["output"] == "file contents here"

    def test_is_error_flips_ok(self) -> None:
        events = _run(
            [
                _tool_result_event(
                    "call_err", "bash: command not found", is_error=True
                ),
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
        )
        payload = next(e for e in events if e.kind is EventKind.TOOL_RESULT).payload
        assert payload["call_id"] == "call_err"
        assert payload["ok"] is False
        assert payload["output"] == "bash: command not found"

    def test_source_call_id_fallback(self) -> None:
        """content[0].toolCallId missing → fall back to message.source.callId."""
        event = _tool_result_event("", "partial output", with_source_call_id="call_src")
        message = event["data"]["message"]
        message["content"][0].pop("toolCallId")

        events = _run([event, {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}])
        payload = next(e for e in events if e.kind is EventKind.TOOL_RESULT).payload
        assert payload["call_id"] == "call_src"

    def test_multiple_text_blocks_joined(self) -> None:
        event = _tool_result_event("call_multi", "part one")
        event["data"]["message"]["content"][0]["content"].append(
            {"type": "text", "text": "part two"}
        )

        events = _run([event, {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}])
        payload = next(e for e in events if e.kind is EventKind.TOOL_RESULT).payload
        assert payload["output"] == "part one\npart two"

    def test_tool_call_mapping_unchanged(self) -> None:
        """TOOL_CALL was already correct (data.callId/name/arguments)."""
        events = _run(
            [
                {
                    "type": "tool/call",
                    "data": {
                        "callId": "call_1",
                        "name": "bash",
                        "arguments": '{"command": "ls"}',
                    },
                },
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
        )
        payload = next(e for e in events if e.kind is EventKind.TOOL_CALL).payload
        assert payload == {
            "call_id": "call_1",
            "name": "bash",
            "arguments": '{"command": "ls"}',
        }


class TestErrorDetailPropagation:
    """Error details must survive the translation layer."""

    def test_turn_end_error_surfaces_message_and_code(self) -> None:
        """turn/end(kind=error) → ERROR event with the real message/code,
        plus the TURN_COMPLETE for turn accounting. On the wire a error
        turn/end implies finish_reason="error" (the SDK derives it from
        the last turn/end kind), so dsh_finish reuses the detail.
        """
        events = _run(
            [
                {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {
                                "message": "MISSING_CREDENTIAL: no api key configured",
                                "code": "MISSING_CREDENTIAL",
                            },
                        }
                    },
                },
            ],
            finish_reason="error",
        )
        detail_errors = [
            e
            for e in events
            if e.kind is EventKind.ERROR
            and e.payload.get("code") == "MISSING_CREDENTIAL"
        ]
        assert len(detail_errors) == 1
        assert detail_errors[0].payload["message"] == (
            "MISSING_CREDENTIAL: no api key configured"
        )
        turn_completes = [e for e in events if e.kind is EventKind.TURN_COMPLETE]
        assert len(turn_completes) == 1
        assert events[-1].kind is EventKind.SESSION_COMPLETE
        assert events[-1].payload["reason"] == "error"
        # The detail arrives before dsh_finish, which reuses the message.
        finish = [
            e
            for e in events
            if e.kind is EventKind.ERROR and e.payload.get("code") == "dsh_finish"
        ]
        assert len(finish) == 1
        assert finish[0].payload["message"] == (
            "MISSING_CREDENTIAL: no api key configured"
        )

    def test_dsh_finish_carries_message(self) -> None:
        """dsh_finish must include a non-empty message (core reads
        payload.get("message", "unknown error")).
        """
        events = _run([], finish_reason="error")
        finish = [
            e
            for e in events
            if e.kind is EventKind.ERROR and e.payload.get("code") == "dsh_finish"
        ]
        assert len(finish) == 1
        assert isinstance(finish[0].payload.get("message"), str)
        assert finish[0].payload["message"]
        assert finish[0].payload["reason"] == "error"

    def test_dsh_finish_reuses_turn_error_message(self) -> None:
        """When the turn already surfaced a detailed error, dsh_finish
        must not fall back to a generic message.
        """
        events = _run(
            [
                {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {
                                "message": "adapter crashed mid-turn",
                                "code": "RUNTIME_ERROR",
                            },
                        }
                    },
                },
            ],
            finish_reason="error",
        )
        finish = [
            e
            for e in events
            if e.kind is EventKind.ERROR and e.payload.get("code") == "dsh_finish"
        ]
        assert len(finish) == 1
        assert finish[0].payload["message"] == "adapter crashed mid-turn"


class TestCostUsageReporting:
    """Real token usage must reach the SESSION_COMPLETE payload."""

    def test_usage_accumulated_across_messages(self) -> None:
        events = _run(
            [
                {
                    "type": "assistant/message",
                    "data": {
                        "message": {"content": [{"type": "text", "text": "a"}]},
                        "usage": {
                            "inputTokens": 100,
                            "outputTokens": 10,
                            "cacheReadTokens": 5,
                            "reasoningTokens": 3,
                        },
                    },
                },
                {
                    "type": "assistant/message",
                    "data": {
                        "message": {"content": []},
                        "usage": {
                            "inputTokens": 50,
                            "outputTokens": 20,
                            "cacheReadTokens": 0,
                            "reasoningTokens": 2,
                        },
                    },
                },
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
        )
        sc = next(e for e in events if e.kind is EventKind.SESSION_COMPLETE)
        assert sc.payload["usage"] == {
            "inputTokens": 150,
            "outputTokens": 30,
            "cacheReadTokens": 5,
            "reasoningTokens": 5,
        }

    def test_usage_absent_yields_no_usage_key(self) -> None:
        """No usage on the wire → payload must not fabricate zeros."""
        events = _run(
            [
                {
                    "type": "assistant/message",
                    "data": {"message": {"content": [{"type": "text", "text": "x"}]}},
                },
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
        )
        sc = next(e for e in events if e.kind is EventKind.SESSION_COMPLETE)
        assert "usage" not in sc.payload

    def test_dead_cost_probe_removed(self) -> None:
        """The _probe_cost_support dead code (checked a nonexistent
        ``sample_run`` attribute) must be gone — the bit is backed by
        real usage data now.
        """
        assert not hasattr(DshSession, "_probe_cost_support")
