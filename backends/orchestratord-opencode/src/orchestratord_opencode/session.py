"""OpenCodeSession — wraps opencode serve HTTP+SSE into the AgentSession SPI.

Scheme B: real SSE translation. ``send()`` opens ``POST /v1/chat``
with ``Accept: text/event-stream`` and parses the response line by
line. Each ``data: {...}`` frame is dispatched to
:meth:`_ingest_sse`, which translates the opencode-native payload
into the normalized :class:`EventKind` set.

Approval flow: when an ``approval.request`` frame arrives we cache it
in :pyattr:`_pending_approvals`; ``approve(request_id, decision)``
posts the decision back to ``/v1/approvals/{request_id}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from orchestratord.spi.approval import ApprovalDecision, ApprovalRequest
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.session import ResumeStatus

logger = logging.getLogger(__name__)

# Total wait for ``opencode serve`` to print its port. The original code
# looped 30 times with a 1s ``wait_for``; we cap the cumulative budget
# at 10s instead so a hung server fails fast.
_SERVER_READY_TIMEOUT_SECONDS = 10.0
# How long an individual ``stderr.readline()`` may block.
_LINE_TIMEOUT_SECONDS = 1.0

# Port-discovery regex for the opencode "listening on http://..." line.
_PORT_RE = re.compile(r":(\d{4,5})")


def _is_sse_response(resp: Any) -> bool:
    """Return True iff the response advertises a Server-Sent-Events stream.

    ``httpx.Response.headers`` is a case-insensitive mapping; we look for
    any ``text/event-stream`` prefix in ``content-type`` so the parser
    does not break if the server adds charset/boundary parameters.
    """
    headers = getattr(resp, "headers", None)
    if headers is None:
        return True  # unknown — assume SSE; downstream will degrade
    content_type = headers.get("content-type", "") if hasattr(headers, "get") else ""
    return "text/event-stream" in content_type.lower()


class OpenCodeSession:
    """Adapts an opencode serve instance into an AgentSession.

    Starts ``opencode serve`` as a subprocess, discovers its port,
    sends prompts via SSE, and consumes ``TEXT_DELTA / TOOL_CALL /
    TOOL_RESULT`` events plus approval hooks.
    """

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = f"oc-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=False,
            approval_hooks=True,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            # opencode serve exposes ``session/load`` MCP
            # call which can probe whether a session is still
            # reachable on the server side. Translates to the
            # three-state ResumeStatus.
            resume_detection=True,
        )
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        self._proc: subprocess.Popen | asyncio.subprocess.Process | None = None
        self._port: int | None = None
        self._client: httpx.AsyncClient | None = None
        self._pending_approvals: dict[str, ApprovalRequest] = {}

    # --- seq / timestamp helpers --------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    # --- server lifecycle ---------------------------------------------

    async def _ensure_server(self) -> tuple[int, httpx.AsyncClient]:
        if (
            self._proc is not None
            and self._port is not None
            and self._client is not None
        ):
            return self._port, self._client

        self._proc = await asyncio.create_subprocess_exec(
            "opencode", "serve", "--port", "0",
            cwd=self._spec.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        port: int | None = None
        deadline = asyncio.get_event_loop().time() + _SERVER_READY_TIMEOUT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(
                    self._proc.stderr.readline(),
                    timeout=_LINE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            if "listening" in decoded.lower() or "http://" in decoded:
                m = _PORT_RE.search(decoded)
                if m:
                    port = int(m.group(1))
                    break

        if port is None:
            # Drain stdout once in case opencode prints the port there.
            try:
                stdout_line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=_LINE_TIMEOUT_SECONDS,
                )
                if stdout_line:
                    m = _PORT_RE.search(stdout_line.decode("utf-8", errors="replace"))
                    if m:
                        port = int(m.group(1))
            except asyncio.TimeoutError:
                pass

        if port is None:
            raise RuntimeError(
                "could not discover opencode serve port within "
                f"{_SERVER_READY_TIMEOUT_SECONDS:.0f}s"
            )

        self._port = port
        self._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
        return port, self._client

    # --- SPI send / events --------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        port, client = await self._ensure_server()
        text = content if isinstance(content, str) else str(content)

        error_emitted = False
        try:
            try:
                async with client.stream(
                    "POST",
                    "/v1/chat",
                    json={"prompt": text, "model": self._spec.model},
                    headers={"Accept": "text/event-stream"},
                    timeout=300.0,
                ) as resp:
                    if resp.status_code != 200 or not _is_sse_response(resp):
                        # Whole response is not SSE — degrade gracefully
                        # (Scheme B §2.6 failure mode: old opencode binary).
                        body = await resp.aread()
                        logger.warning(
                            "opencode returned %s (not SSE); "
                            "falling back to plain text",
                            resp.status_code,
                        )
                        self._events.append(
                            EventEnvelope(
                                seq=self._next_seq(),
                                timestamp=self._now(),
                                kind=EventKind.TEXT,
                                payload={"text": body.decode("utf-8", errors="replace")},
                            )
                        )
                    else:
                        async for line in resp.aiter_lines():
                            payload = self._parse_sse_line(line)
                            if payload is None:
                                continue
                            self._ingest_sse(payload)
            except Exception as exc:
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.ERROR,
                        payload={
                            "code": "opencode_error",
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
                error_emitted = True
        finally:
            # terminal events — TURN_COMPLETE then SESSION_COMPLETE
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TURN_COMPLETE,
                    payload={"reason": "error" if error_emitted else "success"},
                )
            )
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.SESSION_COMPLETE,
                    payload={"reason": "error" if error_emitted else "success"},
                )
            )

    # --- SSE parsing ---------------------------------------------------

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | None:
        """Parse a single SSE line. Returns the decoded payload, or
        ``None`` if the line is a comment / heartbeat / blank.

        Recognized shapes:

        * ``data: {...}`` — single-line JSON payload (current convention)
        * ``data:{...}``  — same line, no space (tolerated)

        Multi-line ``data:`` frames (where each line appends until a
        blank line terminates the event) are not yet exercised by the
        opencode protocol in the wild; if they appear we still parse
        the first ``data:`` line, which is sufficient for the current
        event stream.
        """
        if not line:
            return None
        if line.startswith(":"):
            return None  # SSE comment / keepalive
        if not line.startswith("data:"):
            return None
        payload_text = line[len("data:"):].strip()
        if not payload_text:
            return None
        try:
            obj = json.loads(payload_text)
        except json.JSONDecodeError:
            # Non-JSON SSE; the server fell back to plain text. Stash
            # as a TEXT_DELTA so the consumer still sees it.
            return {"event": "message.delta", "text": payload_text, "delta": payload_text}
        if not isinstance(obj, dict):
            return None
        return obj

    # --- payload → SPI translation ------------------------------------

    def _ingest_sse(self, payload: dict[str, Any]) -> None:
        kind = payload.get("event")
        if kind == "message.delta":
            text = payload.get("text") or payload.get("delta") or ""
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TEXT_DELTA,
                    payload={"text": str(text), "delta": str(text)},
                )
            )
        elif kind == "tool.call":
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TOOL_CALL,
                    payload={
                        "call_id": payload.get("call_id", ""),
                        "name": payload.get("name", ""),
                        "arguments": payload.get("arguments", {}),
                    },
                )
            )
        elif kind == "tool.result":
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TOOL_RESULT,
                    payload={
                        "call_id": payload.get("call_id", ""),
                        "ok": True,
                        "output": payload.get("output"),
                    },
                )
            )
        elif kind == "approval.request":
            request_id = str(payload.get("request_id", ""))
            if not request_id:
                logger.warning("opencode approval.request missing request_id: %s", payload)
                return
            self._pending_approvals[request_id] = ApprovalRequest(
                request_id=request_id,
                call_id=str(payload.get("call_id", "")),
                tool_name=str(payload.get("tool_name", "")),
                arguments=payload.get("arguments", {}) or {},
            )
        elif kind == "turn.complete":
            reason = payload.get("reason", "success")
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TURN_COMPLETE,
                    payload={"reason": str(reason)},
                )
            )
        elif kind == "error":
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={
                        "code": str(payload.get("code", "opencode_error")),
                        "message": str(payload.get("message", "")),
                    },
                )
            )
        # Unknown event kinds are silently dropped — they may be
        # forward-compat additions the orchestrator core has not yet
        # learned about.

    # --- SPI async-iterator -------------------------------------------

    async def _emit_events(self):
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    # --- SPI interrupt / approve / close ------------------------------

    async def interrupt(self) -> None:
        # opencode serve does not expose a wire-level cancel today.
        # The orchestrator core's interrupt-fallback path (mark the
        # turn "abandoned", discard on turn-complete) handles this.
        return None

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        req = self._pending_approvals.pop(request_id, None)
        if req is None:
            logger.warning("approve() called for unknown request_id=%s", request_id)
            return
        if self._client is None:
            logger.warning("approve() called before server is ready")
            return
        try:
            await self._client.post(
                f"/v1/approvals/{request_id}",
                json={
                    "decision": decision.value,
                    "call_id": req.call_id,
                },
            )
        except Exception as exc:
            logger.warning("approve POST failed for %s: %s", request_id, exc)

    async def probe_resume(self) -> ResumeStatus:
        """Probe via opencode ``session/load`` MCP endpoint.

        Strategy: send POST ``/session/load`` with the resume target id;
        the server returns 200 (RESUMED) or 404 (REJECTED). Other
        errors (network, 5xx) collapse to UNDETECTABLE so the caller
        can choose to attempt send() anyway.
        """
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        # Use pre-injected client if a test or caller wired one in
        # directly; otherwise bootstrap the server.
        client = self._client
        if client is None:
            try:
                port, client = await self._ensure_server()
            except Exception as exc:
                logger.warning(
                    "OpenCodeSession.probe_resume: server not ready: %s", exc
                )
                return ResumeStatus.UNDETECTABLE
        try:
            probe_timeout = self._spec.handshake_timeout_s or 30.0
            response = await client.post(
                "/session/load",
                json={"session_id": self._spec.resume_session_id},
                timeout=probe_timeout,
            )
            if response.status_code == 200:
                return ResumeStatus.RESUMED
            if response.status_code == 404:
                return ResumeStatus.REJECTED
            return ResumeStatus.UNDETECTABLE
        except (httpx.TimeoutException, asyncio.TimeoutError):
            logger.warning(
                "OpenCodeSession.probe_resume: HTTP timed out after %.1fs",
                probe_timeout,
            )
            return ResumeStatus.UNDETECTABLE
        except Exception as exc:
            logger.warning(
                "OpenCodeSession.probe_resume: HTTP failed: %s", exc
            )
            return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        self._pending_approvals.clear()
        self._closed = True

    def close_sync(self) -> None:
        if isinstance(self._proc, subprocess.Popen):
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._pending_approvals.clear()
        self._closed = True