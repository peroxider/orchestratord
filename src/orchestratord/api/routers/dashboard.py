"""Legacy LiveView dashboard compat shim (``docs/FEATURE_GAP_VS_MULTICA.md``
§5.5.1, §10.1).

Mirrors the routes previously served by ``cli/dashboard.py``'s
``DashboardHandler`` so existing clients — the chat UI, scripts polling
``/api/state`` and ``/api/runs`` — keep working during the FastAPI split.
Every route reuses the shared ``DashboardState`` + ``ChatGateway`` and the
same module-level helpers the legacy handler used; nothing here re-implements
snapshotting or history reconstruction from scratch.
"""

from __future__ import annotations

import json
import queue
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from orchestratord.api.state import get_dashboard_state
from orchestratord.chat_gateway import ChatGateway
from orchestratord.cli.dashboard import (
    _build_dashboard_html,
    _conversation_history_sessions,
    _conversation_transcript_rows,
    _flatten_previous_run_history,
    _followup_completed_run,
    _snapshot_run_is_active,
)
from orchestratord.conversation_store import ConversationStore
from orchestratord.event_tailer import read_tool_result

router = APIRouter(tags=["dashboard"])


class _MessageBody(BaseModel):
    text: str


class _ControlBody(BaseModel):
    message: str = ""


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
@router.get("/dashboard", include_in_schema=False)
@router.get("/chat", include_in_schema=False)
def dashboard_html() -> HTMLResponse:
    return HTMLResponse(_build_dashboard_html())


# ---------------------------------------------------------------------------
# JSON read endpoints
# ---------------------------------------------------------------------------


@router.get("/api/state")
def api_state() -> dict:
    snap = get_dashboard_state().refresh_snapshot(force=True)
    payload = dict(snap)
    # Flatten token_activity to top level so clients can read
    # active_sessions / total_turns / total_tools directly (§5.2.4).
    payload.update(payload.get("token_activity") or {})
    return payload


@router.get("/api/health")
def api_health() -> dict:
    state = get_dashboard_state()
    return {"ok": True, "workspace": str(state.workspace), "ts": time.time()}


@router.get("/api/issue/{issue_id}")
def api_issue(issue_id: str):
    snap = get_dashboard_state().refresh_snapshot(force=True)
    for issue in snap["issues"]["issues"]:
        if issue["issue_id"] == issue_id:
            return {"issue": issue}
    return JSONResponse({"error": "not found", "issue_id": issue_id}, status_code=404)


@router.get("/api/runs")
def api_runs() -> dict:
    snap = get_dashboard_state().refresh_snapshot(force=True)
    runs = []
    for issue in snap["issues"]["issues"]:
        rid = issue.get("run_id")
        if rid:
            runs.append(
                {
                    "run_id": rid,
                    "conversation_id": issue.get("conversation_id") or rid,
                    "backend": (issue.get("execution") or {}).get("backend", ""),
                    "stage_id": issue.get("stage_id"),
                    "branch_id": issue.get("branch_id"),
                    "issue_id": issue["issue_id"],
                    "status": issue["status"],
                    "workspace_path": issue.get("workspace_path", ""),
                }
            )
    return {"runs": runs, "server_ts": time.time()}


@router.get("/api/conversations")
def api_conversations() -> dict:
    manifests = ConversationStore().list()
    return {
        "conversations": [
            {**manifest, "run_count": len(manifest.get("runs", []))}
            for manifest in manifests
        ],
        "server_ts": time.time(),
    }


@router.get("/api/conversations/{conversation_id}")
def api_conversation(conversation_id: str):
    if not conversation_id or "/" in conversation_id or "\\" in conversation_id:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    manifest = ConversationStore().get(conversation_id)
    if manifest is None:
        manifest = ConversationStore().ensure_legacy_run(conversation_id)
    if manifest is None:
        return JSONResponse(
            {"error": "conversation not found", "conversation_id": conversation_id},
            status_code=404,
        )
    rows = _conversation_transcript_rows(conversation_id)
    return {"conversation": manifest, "events": rows, "conversation_id": conversation_id}


@router.get("/api/runs/{run_id}/observations")
def api_run_observations(run_id: str, cursor: int = 0, limit: int = 500):
    if not run_id or "/" in run_id or "\\" in run_id:
        return JSONResponse({"error": "invalid run id"}, status_code=400)
    state = get_dashboard_state()
    state.refresh_snapshot(force=True)
    return state.observations(run_id, cursor=cursor, limit=limit)


@router.get("/api/runs/{run_id}/tool-results/{call_id}")
def api_tool_result(run_id: str, call_id: str):
    try:
        result = read_tool_result(run_id, call_id)
    except Exception:  # noqa: BLE001 — mirror legacy best-effort lookup
        result = None
    if result is None:
        return JSONResponse(
            {"error": f"tool result {call_id!r} not found for run {run_id!r}"},
            status_code=404,
        )
    return result


# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------


@router.post("/api/conversations/{conversation_id}/messages")
def api_conversation_messages(conversation_id: str, body: _MessageBody):
    if not conversation_id or "/" in conversation_id or "\\" in conversation_id:
        return JSONResponse({"error": "invalid conversation id"}, status_code=400)
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    manifest = ConversationStore().get(conversation_id)
    if manifest is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)

    state = get_dashboard_state()
    runs = manifest.get("runs", [])
    run_id = next(
        (str(run.get("run_id")) for run in reversed(runs) if run.get("status") == "running"),
        None,
    )
    if run_id and state.chat_gateway.send_message(run_id, text):
        return JSONResponse(
            {"accepted": True, "conversation_id": conversation_id, "run_id": run_id},
            status_code=202,
        )
    if runs:
        run_id = str(runs[-1].get("run_id") or "")
        if run_id and _followup_completed_run(state.workspace, run_id, text):
            return JSONResponse(
                {
                    "accepted": True,
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "mode": "followup_queued",
                },
                status_code=202,
            )
    return JSONResponse(
        {"error": "conversation has no writable run", "conversation_id": conversation_id},
        status_code=409,
    )


@router.post("/api/runs/{run_id}/messages")
def api_run_messages(run_id: str, body: _MessageBody):
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    state = get_dashboard_state()
    if state.chat_gateway.send_message(run_id, text):
        return JSONResponse({"accepted": True, "run_id": run_id}, status_code=202)

    snapshot = state.refresh_snapshot(force=True)
    if _snapshot_run_is_active(snapshot, run_id):
        return JSONResponse(
            {
                "error": "run control channel is not ready",
                "run_id": run_id,
                "retryable": True,
            },
            status_code=409,
        )
    queued = _followup_completed_run(state.workspace, run_id, text)
    if not queued:
        return JSONResponse(
            {"error": "run not active and followup queue failed", "run_id": run_id},
            status_code=409,
        )
    return JSONResponse(
        {"accepted": True, "run_id": run_id, "mode": "followup_queued"},
        status_code=202,
    )


@router.post("/api/runs/{run_id}/pause")
@router.post("/api/runs/{run_id}/resume")
@router.post("/api/runs/{run_id}/stop")
def api_run_control(run_id: str, request: Request, body: _ControlBody | None = None):
    verb = request.url.path.rsplit("/", 1)[-1]
    state = get_dashboard_state()
    payload = body.message if body else ""
    ok = state.chat_gateway.control(run_id, verb, payload)
    if not ok:
        return JSONResponse({"error": "run not active", "run_id": run_id}, status_code=409)
    return JSONResponse({"accepted": True, "run_id": run_id}, status_code=202)


@router.post("/api/runs/{run_id}/approve")
@router.post("/api/runs/{run_id}/deny")
def api_run_approval(run_id: str):
    # The persistent approval store (``approvals`` table, §6.1.1) and the
    # pending-approval flow land in Phase 2 (§5.2.3).  Until then there is
    # nothing to approve/deny against, so report it honestly as 404.
    return JSONResponse(
        {"error": "no pending approval for run", "run_id": run_id},
        status_code=404,
    )


# ---------------------------------------------------------------------------
# SSE streams
# ---------------------------------------------------------------------------


def _sse_frame(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"data: {data}\n\n".encode()


def _chat_history_payload(state, gateway: ChatGateway, run_id: str) -> dict[str, Any]:
    history = gateway.read_history(run_id)
    sessions = _conversation_history_sessions(run_id, history)
    for session in sessions:
        coverage = state.observations(str(session.get("run_id") or ""), limit=1)
        session["evidence_count"] = coverage.get("captured_total", 0)
    previous = _flatten_previous_run_history(sessions[:-1])
    if previous:
        history = previous + history
    return {
        "type": "history",
        "messages": history,
        "sessions": sessions,
        "current_run_id": run_id,
    }


@router.get("/api/runs/{run_id}/events")
def api_run_events(run_id: str) -> StreamingResponse:
    return StreamingResponse(
        _run_events_stream(get_dashboard_state(), run_id),
        media_type="text/event-stream",
    )


def _run_events_stream(state, run_id: str) -> Iterator[bytes]:
    gateway: ChatGateway = state.chat_gateway
    live = gateway.subscribe(run_id)
    snapshot = state.refresh_snapshot(force=True)
    if live is None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not _snapshot_run_is_active(snapshot, run_id):
                break
            live = gateway.subscribe(run_id)
            if live is not None:
                break
            time.sleep(0.05)
            snapshot = state.refresh_snapshot(force=True)

    yield _sse_frame(_chat_history_payload(state, gateway, run_id))
    yield _sse_frame({"type": "boundary"})

    if live is None:
        event_type = (
            "RunUnavailable"
            if _snapshot_run_is_active(snapshot, run_id)
            else "RunEnded"
        )
        yield _sse_frame({"type": event_type, "data": {"run_id": run_id}})
        return

    try:
        while True:
            try:
                frame = live.get(timeout=state.snapshot_interval)
            except queue.Empty:
                yield b": ping\n\n"
                continue
            if frame.get("type") == "RunEnded":
                yield _sse_frame(_chat_history_payload(state, gateway, run_id))
                yield _sse_frame({"type": "frame", "frame": frame})
                break
            yield _sse_frame({"type": "frame", "frame": frame})
    finally:
        gateway.unsubscribe(run_id, live)


@router.get("/api/conversations/{conversation_id}/events")
def api_conversation_events(conversation_id: str) -> StreamingResponse:
    return StreamingResponse(
        _conversation_events_stream(get_dashboard_state(), conversation_id),
        media_type="text/event-stream",
    )


def _conversation_events_stream(state, conversation_id: str) -> Iterator[bytes]:
    rows = _conversation_transcript_rows(conversation_id)
    yield _sse_frame(
        {"type": "conversation", "conversation_id": conversation_id, "events": rows}
    )
    yield _sse_frame({"type": "boundary", "conversation_id": conversation_id})
    sent = len(rows)
    while True:
        time.sleep(state.snapshot_interval)
        rows = _conversation_transcript_rows(conversation_id)
        for row in rows[sent:]:
            yield _sse_frame(
                {"type": "event", "conversation_id": conversation_id, "event": row}
            )
        sent = len(rows)
        yield b": ping\n\n"


@router.get("/events")
def api_events_stream() -> StreamingResponse:
    return StreamingResponse(
        _events_stream(get_dashboard_state()), media_type="text/event-stream"
    )


def _events_stream(state) -> Iterator[bytes]:
    snap = state.refresh_snapshot(force=True)
    yield _sse_frame({"type": "snapshot", **snap})
    last_revision = int(snap.get("revision") or 0)
    last_event_cursor = int(snap.get("event_cursor") or 0)
    while True:
        snap = state.refresh_snapshot(force=False)
        revision = int(snap.get("revision") or 0)
        if revision != last_revision:
            yield _sse_frame({"type": "snapshot", **snap})
            last_revision = revision
            last_event_cursor = int(snap.get("event_cursor") or 0)
        else:
            deltas, truncated = state.events_after(last_event_cursor)
            if truncated:
                yield _sse_frame({"type": "snapshot", **snap})
                last_event_cursor = int(snap.get("event_cursor") or 0)
            else:
                for event in deltas:
                    yield _sse_frame(event)
                    last_event_cursor = int(event.get("cursor") or last_event_cursor)
        yield b": ping\n\n"
        time.sleep(state.snapshot_interval)
