"""Orchestratord telemetry recorder — run-level record functions.

Signature-compatible with the clawcodex ``telemetry`` record_* helpers the
orchestrator previously imported, so call sites can switch to this module
without changing arguments. Events are appended to the local JSONL store
(``~/.orchestratord/telemetry/events/``); no remote push happens here
(reporting is a separate aggregation step, phase 2).
"""

from __future__ import annotations

import time
from typing import Any

from .storage import append_event


def _event(event_type: str, **kw: Any) -> dict:
    event = {
        "type": event_type,
        "ts": time.time(),
        "session_id": kw.pop("session_id", "") or "",
    }
    # Run/issue context rides in the payload unless explicitly promoted.
    for k in ("run_id", "issue_id", "backend", "model", "provider"):
        v = kw.pop(k, None)
        if v is not None:
            event[k] = v
    if kw:
        event["payload"] = kw
    return event


def record_session_start(**kw: Any) -> None:
    append_event(_event("session_start", **kw))


def record_session_end(**kw: Any) -> None:
    append_event(_event("session_end", **kw))


def record_command_run(**kw: Any) -> None:
    append_event(_event("command_run", **kw))


def record_error(**kw: Any) -> None:
    # ``exc`` is a common kwarg from callers — serialize as a string so the
    # event stays JSON-serializable.
    exc = kw.pop("exc", None)
    if exc is not None and not isinstance(exc, str):
        kw["exc"] = f"{type(exc).__name__}: {exc}"
    append_event(_event("error", **kw))


def record_turn(**kw: Any) -> None:
    append_event(_event("turn", **kw))


def record_usage(**kw: Any) -> None:
    append_event(_event("usage", **kw))


def record_tool_summary(**kw: Any) -> None:
    append_event(_event("tool_summary", **kw))


def get_recorder():
    """Minimal recorder handle — the module functions record directly.

    Provided for call sites that previously used
    ``telemetry.recorder.get_recorder()`` from the clawcodex package; the
    handle exposes the same record_* methods backed by this module.
    """

    class _Recorder:
        record_session_start = staticmethod(record_session_start)
        record_session_end = staticmethod(record_session_end)
        record_command_run = staticmethod(record_command_run)
        record_error = staticmethod(record_error)
        record_turn = staticmethod(record_turn)
        record_usage = staticmethod(record_usage)
        record_tool_summary = staticmethod(record_tool_summary)

    return _Recorder()
