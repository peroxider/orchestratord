"""AcpSession — generic ACP (Agent Client Protocol) client-side session.

Self-written JSON-RPC 2.0 over stdio (no ``@agentclientprotocol/sdk``
dependency). The session is the ACP **client**: it spawns one ACP agent
subprocess per session and drives it through the wire methods:

* ``initialize`` → handshake (protocolVersion)
* ``session/new`` → create a fresh session (returns ``sessionId``)
* ``session/prompt`` → run one turn; resolves with a stop reason (the
  turn-complete signal)
* ``session/cancel`` → notification; interrupt the in-flight turn

Inbound from the agent:

* ``session/update`` (notification, no id) → streaming deltas / tool
  activity → ``TEXT_DELTA`` / ``TOOL_CALL`` / ``TOOL_RESULT``
* ``session/request_permission`` (request, has id) → ``APPROVAL_REQUEST``;
  :meth:`approve` replies with a ``RequestPermissionOutcome``.

The exact wire shapes are the generic ACP subset documented in
FEATURE_GAP_VS_MULTICA.md §8.3. Per-id defaults (binary + cli_args) come
from :class:`~orchestratord_acp.runtime.AcpRuntime`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision, ApprovalRequest
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

from orchestratord_acp.runtime import AcpRuntime, resolve_binary

logger = logging.getLogger(__name__)

_DEFAULT_HANDSHAKE_TIMEOUT_S = 30.0
_DEFAULT_TOTAL_TIMEOUT_S = 600.0
# The default stop-reason value ACP uses for a normal resolved prompt.
_STOP_REASON_UNKNOWN = "end_turn"


class AcpProtocolError(RuntimeError):
    """The agent sent an unexpected or malformed JSON-RPC frame."""


class AcpSession:
    """Adapt an ACP stdio subprocess into the :class:`AgentSession` SPI."""

    def __init__(self, spec: SessionSpec, runtime: AcpRuntime) -> None:
        self._spec = spec
        self._runtime = runtime
        self.session_id = spec.resume_session_id or f"{runtime.id}-{id(self)}"
        self.conversation_id: str | None = None
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,   # session/update agent_message_chunk
            resumable=False,         # ephemeral stdio subprocess
            interrupt=True,          # session/cancel
            approval_hooks=True,     # session/request_permission
            parallel_sessions=True,  # one process per session
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            goal_mode=False,
            resume_detection=False,  # no cross-process probe path
        )
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._native_session_id: str | None = None
        self._pending_approvals: dict[str, ApprovalRequest] = {}

    # -- seq / timestamp helpers ----------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _now() -> float:
        return time.time()

    def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=kind,
                payload=payload,
            )
        )

    # -- subprocess + handshake -----------------------------------------

    async def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        binary = resolve_binary(self._runtime, self._spec.runtime_bin)
        cmd = [binary, *self._runtime.cli_args]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._spec.cwd or None,
                env=self._spec.env or None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError(
                f"failed to spawn ACP agent {cmd!r}: {exc}"
            ) from exc
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"acp-reader-{self._runtime.id}"
        )
        timeout = self._spec.handshake_timeout_s or _DEFAULT_HANDSHAKE_TIMEOUT_S
        try:
            resp = await self._request(
                "initialize", {"protocolVersion": 1}, timeout_s=timeout
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"ACP agent {self._runtime.id!r} initialize timed out "
                f"after {timeout:.1f}s"
            ) from None
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"ACP agent {self._runtime.id!r} initialize failed: "
                f"{err.get('message', err)}"
            )

    # -- SPI send / events ----------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        text = content if isinstance(content, str) else str(content)

        reason = "success"
        try:
            await self._ensure_started()
            if self._native_session_id is None:
                new_resp = await self._request("session/new", {"cwd": self._spec.cwd})
                if "error" in new_resp:
                    raise AcpProtocolError(
                        f"session/new failed: {new_resp['error']}"
                    )
                self._native_session_id = new_resp.get("result", {}).get(
                    "sessionId"
                ) or "acp-session"

            prompt_resp = await self._request(
                "session/prompt",
                {
                    "sessionId": self._native_session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout_s=self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S,
            )
            if "error" in prompt_resp:
                reason = "error"
                self._emit(
                    EventKind.ERROR,
                    {
                        "code": "acp_prompt_error",
                        "message": str(prompt_resp["error"].get(
                            "message", prompt_resp["error"]
                        )),
                    },
                )
            else:
                stop_reason = prompt_resp.get("result", {}).get(
                    "stopReason", _STOP_REASON_UNKNOWN
                )
                if stop_reason in ("error", "cancel", "cancelled", "max_tokens"):
                    reason = str(stop_reason)
        except asyncio.TimeoutError:
            reason = "timeout"
            self._emit(
                EventKind.ERROR,
                {
                    "code": "acp_timeout",
                    "message": (
                        f"ACP agent {self._runtime.id!r} exceeded the "
                        "turn timeout"
                    ),
                },
            )
        except AcpProtocolError as exc:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": "acp_protocol_error", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {
                    "code": "acp_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )

        self._emit(EventKind.TURN_COMPLETE, {"reason": reason})
        self._emit(EventKind.SESSION_COMPLETE, {"reason": reason})

    async def _emit_events(self) -> AsyncIterator[EventEnvelope]:
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    # -- interrupt / approve / probe / close ----------------------------

    async def interrupt(self) -> None:
        """Send the ACP ``session/cancel`` notification."""
        if self._proc is None or self._native_session_id is None:
            return
        await self._send_notification(
            "session/cancel", {"sessionId": self._native_session_id}
        )

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        req = self._pending_approvals.pop(request_id, None)
        if req is None:
            logger.warning(
                "approve() called for unknown request_id=%s", request_id
            )
            return
        outcome = _decision_to_outcome(decision)
        await self._send_response(int(request_id), {"outcome": outcome})

    async def probe_resume(self) -> ResumeStatus:
        """The stdio subprocess is ephemeral — no cross-process probe."""
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc, self._proc = self._proc, None
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        self._fail_pending(AcpProtocolError("session closed"))
        self._pending_approvals.clear()

    # -- JSON-RPC transport ---------------------------------------------

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        timeout_s: float = _DEFAULT_TOTAL_TIMEOUT_S,
    ) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpProtocolError("agent not started")
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        # NOTE: no lock here — _write_line acquires _write_lock itself, and
        # asyncio.Lock is not reentrant, so wrapping this in the same lock
        # would self-deadlock. The future is registered before the write so
        # a fast response cannot arrive before it is awaited.
        await self._write_line(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            },
            proc,
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise

    async def _send_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        await self._write_line(
            {"jsonrpc": "2.0", "method": method, "params": params}, proc
        )

    async def _send_response(self, req_id: int, result: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        await self._write_line(
            {"jsonrpc": "2.0", "id": req_id, "result": result}, proc
        )

    async def _write_line(
        self, msg: dict[str, Any], proc: asyncio.subprocess.Process
    ) -> None:
        assert proc.stdin is not None
        async with self._write_lock:
            try:
                proc.stdin.write(
                    json.dumps(msg).encode("utf-8") + b"\n"
                )
                await proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:
                raise AcpProtocolError(f"agent stdin write failed: {exc}") from exc

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break  # EOF — agent exited
                try:
                    msg = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.warning("ACP agent sent non-JSON line: %.200r", raw)
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("ACP reader loop crashed")
        finally:
            self._fail_pending(
                AcpProtocolError(f"agent exited (returncode={proc.returncode})")
            )

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg:
            if msg.get("id") is not None:
                self._handle_incoming_request(msg)
            else:
                self._handle_notification(msg)
            return
        req_id = msg.get("id")
        future = self._pending.pop(req_id, None)
        if future is None or future.done():
            return
        if "error" in msg or "result" in msg:
            future.set_result(msg)
        else:
            future.set_exception(
                AcpProtocolError(f"malformed JSON-RPC response: {msg!r:.200}")
            )

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    # -- inbound translation --------------------------------------------

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        if msg.get("method") != "session/update":
            return
        update = (msg.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if text is None:
                text = content if isinstance(content, str) else ""
            self._emit(
                EventKind.TEXT_DELTA,
                {"text": str(text), "delta": str(text)},
            )
        elif kind == "tool_call":
            self._emit(
                EventKind.TOOL_CALL,
                {
                    "call_id": str(update.get("toolCallId", "")),
                    "name": str(update.get("title") or update.get("name") or ""),
                    "arguments": update.get("args") or update.get("input") or {},
                },
            )
        elif kind == "tool_call_update":
            # A completed tool-call update is the closest ACP analogue to a
            # TOOL_RESULT; surface it best-effort.
            status = update.get("status") or {}
            if status.get("status") == "completed":
                self._emit(
                    EventKind.TOOL_RESULT,
                    {
                        "call_id": str(update.get("toolCallId", "")),
                        "ok": True,
                        "output": status.get("result"),
                    },
                )
        else:
            self._emit(
                EventKind.UNKNOWN,
                {"event": kind, "raw": dict(update)},
            )

    def _handle_incoming_request(self, msg: dict[str, Any]) -> None:
        if msg.get("method") != "session/request_permission":
            return
        params = msg.get("params") or {}
        request_id = str(msg.get("id", ""))
        if not request_id:
            return
        tool_call = params.get("toolCall") or {}
        call_id = str(
            tool_call.get("toolCallId")
            or tool_call.get("id")
            or params.get("toolCallId")
            or ""
        )
        tool_name = str(tool_call.get("name") or tool_call.get("title") or "")
        arguments = tool_call.get("args") or tool_call.get("input") or {}
        self._pending_approvals[request_id] = ApprovalRequest(
            request_id=request_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        self._emit(
            EventKind.APPROVAL_REQUEST,
            {
                "request_id": request_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "raw": dict(params),
            },
        )


def _decision_to_outcome(decision: ApprovalDecision) -> dict[str, Any]:
    """Map a four-way :class:`ApprovalDecision` to an ACP ``RequestPermissionOutcome``.

    ACP's outcome is ``{"outcome": {"outcome": "selected" | "cancelled",
    "optionId": str}}``. We map ALLOW/ALWAYS_ALLOW → selected:allow,
    DENY → selected:deny, CANCEL → cancelled. The ``optionId`` strings are
    the generic subset default; agents that advertise concrete option ids
    would need a per-runtime mapping (out of scope for §8.3's generic path).
    """
    if decision in (ApprovalDecision.ALLOW, ApprovalDecision.ALWAYS_ALLOW):
        return {"outcome": {"outcome": "selected", "optionId": "allow"}}
    if decision is ApprovalDecision.DENY:
        return {"outcome": {"outcome": "selected", "optionId": "deny"}}
    return {"outcome": {"outcome": "cancelled"}}
