"""Runner utility functions — extracted from AgentRunner static methods.

These functions operate on ``AgentSession`` fields and have no dependency
on any concrete backend package.

Extracted from ``agent_runner.py`` during Phase C of the orchestratord
decoupling refactor.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


async def _await_with_active_timeout(awaitable, session: Any, timeout: float):
    """Bound active wall time, excluding confirmed operator pauses.

    Cancellation still tears down the child coroutine and its backend. Never
    restart work merely because a polling interval expired.
    """
    import time

    task = asyncio.ensure_future(awaitable)
    remaining = timeout
    previous = time.monotonic()
    was_paused = bool(getattr(session, "paused", False))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=.02)
            now = time.monotonic()
            if not was_paused:
                remaining -= now - previous
            previous = now
            was_paused = bool(getattr(session, "paused", False))
            session.timeout_deadline_at = None if was_paused else time.time() + max(0, remaining)
            if done:
                return await task
            if remaining <= 0 and not was_paused:
                raise TimeoutError("Active run timeout exceeded")
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
        # Preserve the provider payload first.  The aliases below keep the
        # pre-v2 browser vocabulary stable while making every provider field
        # available to transcript consumers and future renderers.
        data = dict(payload)
        if kind_value in ("text", "text_delta"):
            data.setdefault("content", payload.get("text", payload.get("delta", "")))
            # Keep the original minimal wire shape for ordinary text deltas;
            # richer/provider-specific payloads still retain every field.
            if set(payload).issubset({"text", "delta", "turn", "timestamp_quality"}):
                data = {
                    "content": data["content"],
                    **{key: payload[key] for key in ("turn", "timestamp_quality") if key in payload},
                }
        elif kind_value == "tool_call":
            arguments = payload.get("arguments", payload.get("input", payload.get("params", {})))
            data.setdefault("tool_name", payload.get("name", payload.get("tool_name", "")))
            data.setdefault("tool_use_id", payload.get(
                "call_id", payload.get("tool_use_id", payload.get("toolCallId", payload.get("callId")))
            ))
            data.setdefault("params", dict(arguments) if isinstance(arguments, Mapping) else arguments)
        elif kind_value == "tool_result":
            data.setdefault("tool_use_id", payload.get(
                "call_id", payload.get("tool_use_id", payload.get("toolCallId", payload.get("callId")))
            ))
            data.setdefault("result", payload.get("result", payload.get("output", payload)))
            data.setdefault("is_error", not bool(payload.get("ok", True)))
        elif kind_value in ("error", "approval_request"):
            # Stable control-socket consumers historically read ``code`` and
            # ``message`` without checking presence first.
            data.setdefault("code", "")
            data.setdefault("message", "")
        return data

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
    The whole method is wrapped in try/except.

    The transcript frame is written even when no control socket
    is attached — the transcript is the backing store for ``issue
    tail`` / ``issue transcript`` / ``run logs`` and must not depend on
    a socket client being connected.
    """
    try:
        kind = getattr(event, "kind", None)
        kind_value = getattr(kind, "value", kind)
        type_map = {
            "session_started": "SessionStarted",
            "text": "TextDelta",
            "text_delta": "TextDelta",
            "tool_call": "ToolCallEvent",
            "tool_result": "ToolResultEvent",
            "turn_complete": "TurnComplete",
            "session_complete": "SessionComplete",
            "error": "Error",
            "approval_request": "ApprovalRequest",
            "phase_complete": "PhaseComplete",
            "unknown": "UnknownEvent",
        }
        frame = {
            "type": type_map.get(kind_value, event.__class__.__name__),
            "data": _event_to_broadcast_dict(event),
        }
        # New consumers can join the logical conversation directly; legacy
        # consumers that construct bare sessions retain the old frame shape.
        if getattr(session, "conversation_id", None) or getattr(session, "run_id", None):
            frame.update({
                "conversation_id": getattr(session, "conversation_id", None),
                "run_id": getattr(session, "run_id", None),
                "backend": getattr(session, "backend_name", None),
                "backend_session_id": getattr(session, "backend_session_id", None),
                "stage_id": getattr(session, "stage_id", None),
                "branch_id": getattr(session, "branch_id", None),
                "parent_run_id": getattr(session, "parent_run_id", None),
            })
        conversation_id = getattr(session, "conversation_id", None)
        if conversation_id and getattr(session, "run_id", None):
            try:
                from .conversation_store import ConversationStore

                ConversationStore().register_run(
                    conversation_id=conversation_id,
                    run_id=session.run_id,
                    backend=getattr(session, "backend_name", None),
                    backend_session_id=getattr(session, "backend_session_id", None),
                    issue_id=getattr(getattr(session, "issue", None), "id", None),
                    parent_run_id=getattr(session, "parent_run_id", None),
                    stage_id=getattr(session, "stage_id", None),
                    stage_name=getattr(session, "stage_name", None),
                    branch_id=getattr(session, "branch_id", None),
                )
            except Exception:
                logger.debug("conversation manifest event update failed", exc_info=True)
        frame["ts"] = getattr(event, "timestamp", None)
        await _publish_transcript_frame(session, frame, event=event)
    except Exception:
        pass


async def _publish_transcript_frame(
    session: Any, frame: dict, *, event: Any | None = None
) -> None:
    """Persist before live delivery; a disconnected viewer cannot lose history."""
    if getattr(session, "conversation_id", None) or getattr(session, "run_id", None):
        frame.update({
            "conversation_id": getattr(session, "conversation_id", None),
            "run_id": getattr(session, "run_id", None),
            "backend": getattr(session, "backend_name", None),
            "backend_session_id": getattr(session, "backend_session_id", None),
            "stage_id": getattr(session, "stage_id", None),
            "branch_id": getattr(session, "branch_id", None),
            "parent_run_id": getattr(session, "parent_run_id", None),
        })
    _write_transcript_frame(
        getattr(session, "run_id", None), frame, session=session, event=event
    )
    if getattr(session, "control_socket", None) is not None:
        try:
            await session.control_socket.send_event({key: value for key, value in frame.items() if key != "ts"})
        except Exception:
            logger.debug("Live transcript delivery failed", exc_info=True)


def _transcript_message_from_frame(frame: dict) -> dict:
    """Convert a control-socket frame into a claude-style transcript
    message (``role`` + ``content`` blocks).

    Transcript rows historically had no ``role`` field, so
    ``issue transcript --role assistant`` filtered everything out and
    the readers degenerated to ``role='?'``. The mapping follows the
    claude-code transcript convention the readers are built for:
    assistant messages carry text / tool_use blocks, user messages
    carry tool_result blocks, and lifecycle events are ``system``.
    """
    frame_type = frame.get("type")
    data = frame.get("data") if isinstance(frame.get("data"), dict) else {}

    if frame_type == "RunInput":
        return {
            "type": "RunInput",
            "schema_version": 1,
            "role": "user",
            "content": [{"type": "text", "text": str(data.get("content", ""))}],
            "origin": data.get("origin", "orchestrator"),
            "system_prompt": data.get("system_prompt") or "",
            "delivery": "submitted",
            "backend": data.get("backend") or "",
            "resume_session_id": data.get("resume_session_id"),
        }
    if frame_type == "TextDelta":
        return {
            "type": "TextDelta",
            "role": "assistant",
            "content": [{"type": "text", "text": str(data.get("content", ""))}],
            "turn": data.get("turn"),
            "timestamp_quality": data.get("timestamp_quality"),
        }
    if frame_type == "ToolCallEvent":
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": str(
                        data.get("tool_use_id")
                        or data.get("call_id")
                        or data.get("toolCallId")
                        or data.get("callId")
                        or ""
                    ),
                    "name": str(data.get("tool_name", "")),
                    "input": data.get("params", {}),
                    "turn": data.get("turn"),
                }
            ],
            "timestamp_quality": data.get("timestamp_quality"),
        }
    if frame_type == "ToolResultEvent":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": str(
                        data.get("tool_use_id")
                        or data.get("call_id")
                        or data.get("toolCallId")
                        or data.get("callId")
                        or ""
                    ),
                    "content": data.get("result"),
                    "is_error": data.get("is_error", False),
                    "turn": data.get("turn"),
                }
            ],
            "timestamp_quality": data.get("timestamp_quality"),
        }
    if frame_type == "InjectDelivered":
        return {
            "role": "user",
            "content": [{"type": "text", "text": str(data.get("hint_snippet", ""))}],
            "origin": "inject",
        }
    if frame_type == "UserMessage":
        return {
            "role": "user",
            "content": [{"type": "text", "text": str(data.get("content", ""))}],
            "origin": data.get("origin", "prompt"),
        }
    if frame_type == "Error":
        return {
            "type": "Error",
            "data": data,
            "role": "system",
            "content": [
                {"type": "text", "text": str(data.get("message", "Backend error"))}
            ],
            "code": str(data.get("code", "backend_error")),
        }
    # Lifecycle / unknown events: keep the raw frame for audit value but
    # give it a role so the readers can filter deterministically.
    msg = dict(frame)
    msg["role"] = "system"
    return msg


def _write_transcript_frame(
    run_id: str | None,
    frame: dict,
    *,
    session: Any | None = None,
    event: Any | None = None,
) -> None:
    """Append a frame to the session transcript JSONL file.

    The frame is stored as a claude-style message (see
    :func:`_transcript_message_from_frame`). Best-effort: failures are
    logged so a full disk or permission error never breaks
    the agent run.
    """
    if not run_id:
        return
    try:
        import time as _time
        from pathlib import Path as _Path

        sessions_dir = _Path.home() / ".orchestratord" / "sessions"
        transcript_path = sessions_dir / run_id / "transcript.jsonl"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        entry = _transcript_message_from_frame(frame)
        entry["ts"] = frame.get("ts") or _time.time()
        data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
        kind = getattr(getattr(event, "kind", None), "value", None) or str(frame.get("type", "unknown"))
        entry.update({
            "schema_version": 2,
            "conversation_id": getattr(session, "conversation_id", None),
            "run_id": run_id,
            "backend": getattr(session, "backend_name", None),
            "backend_session_id": getattr(session, "backend_session_id", None),
            "stage_id": getattr(session, "stage_id", None),
            "stage_name": getattr(session, "stage_name", None),
            "branch_id": getattr(session, "branch_id", None),
            "parent_run_id": getattr(session, "parent_run_id", None),
            "seq": getattr(event, "seq", None),
            "timestamp": getattr(event, "timestamp", None) or entry["ts"],
            "kind": kind,
        })
        # Keep the normalized projection flat for old readers, while also
        # exposing the v2 family envelopes for field-presence rendering.
        for key, value in data.items():
            if key not in {"content"} or "content" not in entry:
                entry[key] = value
        if kind in ("text", "text_delta"):
            entry.setdefault("text", data.get("text", data.get("delta", data.get("content", ""))))
            if kind == "text_delta":
                entry.setdefault("delta", data.get("delta", data.get("text", data.get("content", ""))))
        if kind in ("tool_call", "tool_result") or any(
            key in data for key in ("call_id", "callId", "tool_use_id", "toolCallId", "name", "tool_name")
        ):
            entry["tool"] = {
                "call_id": data.get(
                    "call_id", data.get("tool_use_id", data.get("toolCallId", data.get("callId")))
                ),
                "name": data.get("name", data.get("tool_name")),
                "arguments": data.get("arguments", data.get("input", data.get("params"))),
                "result": data.get("result", data.get("output")),
                "output": data.get("output"),
                "ok": data.get("ok"),
                "is_error": data.get("is_error", data.get("isError")),
            }
        if kind.startswith("approval") or any(key in data for key in ("request_id", "decision", "deny_reason")):
            entry["approval"] = {key: data.get(key) for key in (
                "request_id", "call_id", "callId", "tool_name", "arguments", "message", "decision", "deny_reason"
            )}
        if any(key in data for key in ("thinking", "reasoning", "reasoning_text", "reasoning-delta")):
            entry["reasoning"] = {
                "text": data.get(
                    "thinking", data.get("reasoning", data.get("reasoning_text", data.get("reasoning-delta")))
                ),
                "is_summary": bool(data.get("is_summary", False)),
            }
        if isinstance(data.get("usage"), dict) or any(key in data for key in ("input_tokens", "output_tokens", "total_tokens", "total_cost_usd")):
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            entry["usage"] = {
                "input_tokens": usage.get("input_tokens", data.get("input_tokens")),
                "output_tokens": usage.get("output_tokens", data.get("output_tokens")),
                "total_tokens": usage.get("total_tokens", data.get("total_tokens")),
                "cost_usd": usage.get("cost_usd", data.get("total_cost_usd")),
            }
        if any(key in data for key in ("turn", "turn_delta", "phase", "status", "reason", "error_code", "error_message", "code", "message")):
            entry["lifecycle"] = {key: data.get(key) for key in (
                "turn", "turn_delta", "phase", "turn_count", "reason", "status", "code", "error_code", "error_message", "message"
            )}
        if "raw" in data and data.get("raw") is not None:
            entry["raw"] = data["raw"]
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("Unable to persist transcript for run %s", run_id, exc_info=True)


# ---------------------------------------------------------------------------
# Pause / resume helpers
# ---------------------------------------------------------------------------


def _apply_pause_session(session: Any, reason: str = "operator_interrupt") -> None:
    """Apply pause state to a session (shared by socket + control-file paths).

    Sets ``paused`` and clears any backend-supplied pause gates.  The generic
    BackendRunner always stops consuming events at the next boundary; a
    backend already executing an operation may finish that operation unless
    it supplied a stronger native gate.  Does **not** notify the registry —
    callers are responsible for that via
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

    Sets ``paused = False`` and restores any backend-supplied gates so event
    consumption can resume.  Does **not** notify the registry — callers are
    responsible for that.
    """
    if prompt_override:
        session.prompt_override = prompt_override
    session.paused = False
    session.pause_reason = ""
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


def _drain_control_commands(session: Any, *, commands: list | None = None) -> bool:
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
        supplied = iter(commands) if commands is not None else None
        while True:
            try:
                cmd = next(supplied) if supplied is not None else _q.get_nowait()
            except (asyncio.QueueEmpty, StopIteration):
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
                session.paused = False
                session.pause_reason = ""
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
