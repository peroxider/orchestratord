"""Runner utility functions — extracted from AgentRunner static methods.

These functions operate on ``AgentSession`` fields and have no dependency
on any concrete backend package.

Extracted from ``agent_runner.py`` during Phase C of the orchestratord
decoupling refactor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event serialisation for control-socket broadcast
# ---------------------------------------------------------------------------


def _event_to_broadcast_dict(event: Any) -> dict:
    """JSON-safe dict representation of a query event.

    Used by ``ControlSocket.send_event`` to broadcast to attached
    clients. Defensive: never raises; missing fields are omitted
    so a partial event still serializes cleanly.

    Event types are imported from orchestratord's own event model. Unknown
    events fall back to duck-typing for SPI ``EventEnvelope`` objects.
    """
    # BackendRunner feeds this helper SPI EventEnvelope values.  Convert
    # them to the stable control-socket vocabulary rather than exposing the
    # SPI implementation class name to the browser.
    kind = getattr(event, "kind", None)
    payload = getattr(event, "payload", None)
    if kind is not None and isinstance(payload, dict):
        kind_value = getattr(kind, "value", kind)
        if kind_value in ("text", "text_delta"):
            return {"content": str(payload.get("text", payload.get("delta", "")))}
        if kind_value == "tool_call":
            return {
                "tool_name": str(payload.get("name", payload.get("tool_name", ""))),
                "tool_use_id": payload.get("call_id", payload.get("tool_use_id")),
                "params": dict(payload.get("arguments", payload.get("params", {})) or {}),
            }
        if kind_value == "tool_result":
            return {
                "tool_name": str(payload.get("name", payload.get("tool_name", ""))),
                "tool_use_id": payload.get("call_id", payload.get("tool_use_id")),
                "result": payload.get("result", payload),
            }
        if kind_value == "turn_complete":
            return {"turn": payload.get("turn", 0)}
        if kind_value == "session_complete":
            return {"reason": str(payload.get("reason", ""))}

    try:
        from orchestratord.events.agent_events import (
            PhaseComplete,
            SessionComplete,
            TextDelta,
            ToolCallEvent,
            ToolResultEvent,
            TurnComplete,
        )
    except ImportError:
        # Fallback for backend-specific SPI EventEnvelope objects.
        kind = getattr(event, "kind", None)
        if kind is not None:
            payload = getattr(event, "payload", {})
            return dict(payload) if payload else {}
        return {}

    if isinstance(event, TextDelta):
        return {"content": str(getattr(event, "content", ""))}
    if isinstance(event, ToolCallEvent):
        return {
            "tool_name": str(getattr(event, "tool_name", "")),
            "tool_use_id": getattr(event, "tool_use_id", None),
            "params": dict(getattr(event, "params", {}) or {}),
            "approved": getattr(event, "_approved", None),
        }
    if isinstance(event, ToolResultEvent):
        return {
            "tool_name": str(getattr(event, "tool_name", "")),
            "tool_use_id": getattr(event, "tool_use_id", None),
            "result": dict(getattr(event, "result", {}) or {}),
        }
    if isinstance(event, PhaseComplete):
        return {
            "phase": getattr(event, "phase", 0),
            "turn_count": getattr(event, "turn_count", 0),
        }
    if isinstance(event, TurnComplete):
        return {"turn": getattr(event, "turn", 0)}
    if isinstance(event, SessionComplete):
        return {"reason": str(getattr(event, "reason", ""))}
    return {}


async def _broadcast_to_socket(session: Any, event: Any) -> None:
    """Broadcast an event to attached control-socket clients.

    Defensive: a broken socket must never abort the agent run.
    The whole method is wrapped in try/except and guarded by
    ``is not None``.
    """
    if session.control_socket is None:
        return
    try:
        kind = getattr(event, "kind", None)
        kind_value = getattr(kind, "value", kind)
        type_map = {
            "text": "TextDelta",
            "text_delta": "TextDelta",
            "tool_call": "ToolCallEvent",
            "tool_result": "ToolResultEvent",
            "turn_complete": "TurnComplete",
            "session_complete": "SessionComplete",
        }
        await session.control_socket.send_event(
            {
                "type": type_map.get(kind_value, event.__class__.__name__),
                "data": _event_to_broadcast_dict(event),
            }
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pause / resume helpers
# ---------------------------------------------------------------------------


def _apply_pause_session(session: Any, reason: str = "operator_interrupt") -> None:
    """Apply pause state to a session (shared by socket + control-file paths).

    Sets ``paused``, clears ``pause_resume_event`` and ``_pause_gate``
    so the agent stops making LLM API calls.  Does **not** notify the
    registry — callers are responsible for that via
    ``_on_pause_state_change`` (socket path) or ``registry.mark_paused``
    (control-file path).
    """
    session.paused = True
    session.pause_reason = reason
    if session.pause_resume_event is not None:
        session.pause_resume_event.clear()
    if session._pause_gate is not None:
        session._pause_gate.clear()
    # Drop cached tracker snapshots on operator pause so the
    # post-resume ``_should_continue`` re-polls the tracker instead of
    # trusting pre-pause state (the operator may have mutated the
    # issue while the session was paused).
    if session.state_cache is not None:
        session.state_cache.invalidate()


def _apply_resume_session(session: Any, prompt_override: str | None = None) -> None:
    """Apply resume state to a session (shared by socket + control-file paths).

    Sets ``paused = False``, restores ``pause_resume_event`` and
    ``_pause_gate`` so the agent resumes LLM API calls.  Does **not**
    notify the registry — callers are responsible for that.
    """
    if prompt_override:
        session.prompt_override = prompt_override
    session.paused = False
    if session.pause_resume_event is not None:
        session.pause_resume_event.set()
    if session._pause_gate is not None:
        session._pause_gate.set()


# ---------------------------------------------------------------------------
# Operator hint (shared by socket-based and file-based inject paths)
# ---------------------------------------------------------------------------


def _write_operator_hint(session: Any, hint: str) -> None:
    """Write an operator hint to ``.operator_hints.md``.

    Mirrors the format used by ``issue.py:_inject_hint`` so the
    file-based and socket-based inject paths converge. Best-effort:
    failures are logged but never propagate. Idempotent: skips
    if the hint text already exists in the file.
    """
    if not hint or not session.run_id:
        return
    try:
        import time as _time
        from pathlib import Path as _Path

        ws_path = getattr(session.workspace, "path", None)
        if not ws_path:
            return
        hints_file = _Path(str(ws_path)) / ".operator_hints.md"
        # Count existing hints for the numbering.
        next_num = 1
        if hints_file.exists():
            content = hints_file.read_text(encoding="utf-8")
            # Idempotency — skip if the hint
            # text already exists (same check as issue.py).
            if hint.strip() in content:
                return
            next_num = content.count("--- Operator Hint #") + 1
        timestamp = _time.strftime("%Y-%m-%d %H:%M:%S")
        header = f"--- Operator Hint #{next_num} (injected at {timestamp}) ---\n"
        separator = "-" * 50 + "\n"
        with open(hints_file, "a", encoding="utf-8") as f:
            f.write(header)
            f.write(hint + "\n")
            f.write(separator)
    except Exception:
        logger.exception(
            "Failed to write operator hint run_id=%s",
            session.run_id,
        )


# ---------------------------------------------------------------------------
# Control-socket command drain
# ---------------------------------------------------------------------------


def _drain_control_commands(session: Any) -> bool:
    """Drain pending control-socket commands non-blockingly.

    Returns ``True`` if ``stop`` or ``takeover`` was received,
    signaling the caller should break out of the event stream
    loop. ``pause`` / ``resume`` / ``inject`` / ``detach`` are
    handled inline and never request an early break.

    ``inject`` now writes to ``.operator_hints.md``
    (converging with the file-based ``issue inject`` path).
    ``stop`` now sets ``session.status = "failed"`` (was
    metadata-only). ``detach`` is logged (basic version).
    """
    if session.control_socket is None:
        return False
    stop_requested = False
    try:
        _q = session.control_socket._command_queue
        while True:
            try:
                cmd = _q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if cmd.cmd == "pause":
                _apply_pause_session(session, "operator_interrupt")
                # Notify orchestrator to sync registry status.
                if session._on_pause_state_change is not None:
                    try:
                        session._on_pause_state_change(
                            session.issue.id if session.issue else "",
                            True,
                            session.pause_reason,
                        )
                    except Exception:
                        logger.exception("_on_pause_state_change failed")
            elif cmd.cmd == "resume":
                _apply_resume_session(session, cmd.payload)
                # Notify orchestrator to sync registry status.
                if session._on_pause_state_change is not None:
                    try:
                        session._on_pause_state_change(
                            session.issue.id if session.issue else "",
                            False,
                            "",
                        )
                    except Exception:
                        logger.exception("_on_pause_state_change failed")
            elif cmd.cmd == "stop":
                session.status = "failed"
                session.session_end_reason = "operator_stop"
                session.session_end_summary = "operator sent stop via control socket"
                if session.pause_resume_event is not None:
                    session.pause_resume_event.set()
                stop_requested = True
            elif cmd.cmd == "takeover":
                session.status = "failed"
                session.session_end_reason = "operator_takeover"
                session.session_end_summary = "operator requested takeover via control socket"
                stop_requested = True
            elif cmd.cmd == "inject":
                # Write the message to the transcript as a UserMessage
                # so it appears in the conversation history (visible
                # via takeover REPL). The agent is expected to be
                # paused when this command arrives (the CLI sends
                # pause → inject → resume).
                if session._transcript_storage is not None:
                    try:
                        from orchestratord.events.agent_events import (
                            TextBlock,
                            create_user_message,
                        )

                        session._transcript_storage.write_message(
                            create_user_message(
                                content=[TextBlock(text=cmd.payload)],
                                origin="inject",
                            ),
                        )
                        session._transcript_storage.flush()
                        logger.info(
                            "inject written to transcript run_id=%s len=%d",
                            session.run_id,
                            len(cmd.payload),
                        )
                    except ImportError:
                        logger.debug(
                            "inject: transcript storage unavailable"
                        )
                    except Exception:
                        logger.exception(
                            "Failed to write inject to transcript run_id=%s",
                            session.run_id,
                        )

                # Queue for backend-agnostic prompt injection on the next
                # turn. Keep the operator-hints file as a durable fallback.
                session._pending_followups.append(cmd.payload)
                _write_operator_hint(session, cmd.payload)

                # Emit InjectDelivered immediately — the CLI waits
                # for this confirmation.
                if session.control_socket is not None:
                    try:
                        import asyncio as _inject_asyncio

                        _snippet = cmd.payload[:80] if cmd.payload else ""
                        _coro = session.control_socket.send_event(
                            {
                                "type": "InjectDelivered",
                                "data": {
                                    "hint_snippet": _snippet,
                                },
                            },
                        )
                        try:
                            _loop = _inject_asyncio.get_running_loop()
                            _loop.create_task(_coro)
                        except RuntimeError:
                            _inject_asyncio.run(_coro)
                    except Exception:
                        logger.exception("Failed to emit InjectDelivered")
            elif cmd.cmd == "detach":
                logger.info(
                    "control_socket detach received run_id=%s",
                    session.run_id,
                )
            elif cmd.cmd == "followup":
                # Write the message to the transcript as a UserMessage
                # so it appears in conversation history (chat replay).
                if session._transcript_storage is not None:
                    try:
                        from orchestratord.events.agent_events import (
                            TextBlock,
                            create_user_message,
                        )

                        session._transcript_storage.write_message(
                            create_user_message(
                                content=[TextBlock(text=cmd.payload)],
                                origin="followup",
                            ),
                        )
                        session._transcript_storage.flush()
                    except ImportError:
                        logger.debug(
                            "followup: transcript storage unavailable"
                        )
                    except Exception:
                        logger.exception(
                            "Failed to write followup to transcript run_id=%s",
                            session.run_id,
                        )

                # Queue for backend-agnostic prompt injection next turn.
                session._pending_followups.append(cmd.payload)

                # Write .operator_hints.md as a durable fallback so the
                # followup survives agent crashes (mirrors the inject path).
                _write_operator_hint(session, cmd.payload)

                # Broadcast FollowupQueued confirmation frame.
                if session.control_socket is not None:
                    try:
                        import asyncio as _followup_asyncio

                        _snippet = cmd.payload[:80] if cmd.payload else ""
                        _coro = session.control_socket.send_event(
                            {
                                "type": "FollowupQueued",
                                "data": {
                                    "snippet": _snippet,
                                },
                            },
                        )
                        try:
                            _loop = _followup_asyncio.get_running_loop()
                            _loop.create_task(_coro)
                        except RuntimeError:
                            _followup_asyncio.run(_coro)
                    except Exception:
                        logger.exception("Failed to emit FollowupQueued")
            elif cmd.cmd == "flush_transcript":
                if session._transcript_storage is not None:
                    try:
                        session._transcript_storage.flush()
                        logger.info(
                            "Transcript flushed on request run_id=%s",
                            session.run_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to flush transcript run_id=%s",
                            session.run_id,
                        )
    except Exception:
        logger.exception("control_socket drain failed")
    return stop_requested
