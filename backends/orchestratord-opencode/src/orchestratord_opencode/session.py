"""OpenCodeSession — wraps opencode serve HTTP+SSE into the AgentSession SPI.

Real opencode HTTP protocol (verified live against opencode 1.18.27,
``GET /doc`` OpenAPI + frame-level probes):

  POST /api/session                              create session (``ses_*``)
  POST /api/session/{id}/prompt                  submit prompt (async admit)
  GET  /api/event                                global SSE bus — the primary
                                                 event source. It carries the
                                                 full ``session.next.*``
                                                 vocabulary **including
                                                 text/reasoning/tool deltas**
                                                 plus ``permission.v2.*``
                                                 approval events; every frame
                                                 is attributed via
                                                 ``data.sessionID``.
  POST /api/session/{id}/permission/{req}/reply  approval decision
                                                 (``once``/``always``/``reject``)
  POST /api/session/{id}/interrupt               interrupt the running turn
  GET  /api/session/{id}                        resume probe (404 = gone)

send() is **non-blocking** (dsh pattern): it spawns the turn task and
returns immediately. The orchestration core consumes ``events()``
sequentially *after* ``send()`` returns (backend_runner), so streaming
metrics, the five-level timeouts and the approval round-trip only work
when events flow concurrently with the turn — hence the asyncio.Queue
bridge.

Approval flow: a ``permission.v2.asked`` frame for this session is
translated into an APPROVAL_REQUEST envelope immediately (the core
policy is authoritative); ``approve()`` posts the decision back to the
reply endpoint (allow→``once``, always_allow→``always``,
deny/cancel→``reject``).

Model selection: ``spec.model`` uses the ``"{providerID}/{modelID}"``
convention (e.g. ``火山AI网关/deepseek-v4-flash``); without a slash the
server default is used (logged — the free default model is rate-limited).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx

from orchestratord.spi.approval import ApprovalDecision, ApprovalRequest
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

logger = logging.getLogger(__name__)

# Total wait for ``opencode serve`` to print its port banner. Node boot
# takes a few seconds; 15s covers cold starts while still failing fast.
_SERVER_READY_TIMEOUT_SECONDS = 15.0
# How long an individual stdout ``readline()`` may block while polling
# for the banner.
_LINE_TIMEOUT_SECONDS = 1.0

# Banner shape: "opencode server listening on http://127.0.0.1:40961"
# (printed on **stdout**; stderr stays empty).
_PORT_RE = re.compile(r"listening on \S*:(\d+)")

# opencode session ids look like ``ses_...`` (OpenAPI pattern "^ses").
_OC_SESSION_ID_RE = re.compile(r"^ses")

_DEFAULT_CONNECT_TIMEOUT = 10.0

# After a permission DENY opencode fails the tool ("Tool execution
# interrupted") and **abandons the turn entirely** — no step.ended, no
# further model calls, interrupt() cannot unwind it (verified live
# against 1.18.29). The backend therefore synthesizes the turn
# termination after this grace window; model-level activity within the
# window clears it (forward-compatible with an upstream fix).
_DENY_GRACE_SECONDS = 10.0


class _TransportError(Exception):
    """Backend-level transport failure carrying an SPI error ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_sse_response(resp: Any) -> bool:
    """True iff the response advertises a Server-Sent-Events stream."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return True  # unknown — assume SSE; downstream will degrade
    content_type = headers.get("content-type", "") if hasattr(headers, "get") else ""
    return "text/event-stream" in content_type.lower()


class OpenCodeSession:
    """Adapts one ``opencode serve`` instance into an AgentSession.

    Server lifecycle: each OpenCodeSession spawns its own serve
    subprocess (process-level isolation) on an explicitly selected free
    port — ``--port 0`` would prefer opencode's fixed default 4096 and
    break parallel sessions.
    """

    def __init__(
        self,
        spec: SessionSpec,
        client_factory: Any | None = None,
    ) -> None:
        self._spec = spec
        self._client_factory = client_factory
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
            # GET /api/session/{id} answers the resume probe.
            resume_detection=True,
        )
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self._seq = 0
        self._closed = False
        self._turn_started = False
        self._turn_task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._port: int | None = None
        self._client: httpx.AsyncClient | None = None
        # opencode-side session id (ses_*), created on the first turn.
        self._oc_session_id: str | None = None
        self._pending_approvals: dict[str, ApprovalRequest] = {}
        # request_id → tool_name (from permission.v2.asked) — used to
        # give the synthesized deny-failure a concrete tool name.
        self._asked_tools: dict[str, str] = {}
        # Deny-abandonment watch: armed on permission.v2.replied(reject),
        # disarmed by model-level activity, fires → synthesize terminal.
        self._deny_grace_deadline: float | None = None
        self._deny_tool_name: str = "unknown"
        # callID → parsed tool input (from tool.called / tool.input.ended),
        # used to enrich APPROVAL_REQUEST arguments.
        self._tool_inputs: dict[str, Any] = {}
        # textIDs that already produced deltas this turn — their
        # ``text.ended`` must not be re-emitted as a full TEXT event.
        self._text_delta_ids: set[str] = set()
        self._usage_totals: dict[str, int] = {}
        self._last_error_message: str | None = None

    # --- seq / timestamp helpers --------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=kind,
                payload=payload,
            )
        )

    # --- timeout helpers -----------------------------------------------

    def _handshake_timeout(self) -> float:
        return float(self._spec.handshake_timeout_s or 30.0)

    def _read_timeout(self) -> float:
        return float(self._spec.inactivity_timeout_s or 300.0)

    def _http_timeout(self, *, read: float | None = None) -> httpx.Timeout:
        return httpx.Timeout(
            connect=_DEFAULT_CONNECT_TIMEOUT,
            read=self._read_timeout() if read is None else read,
            write=30.0,
            pool=30.0,
        )

    # --- server lifecycle ----------------------------------------------

    @staticmethod
    def _pick_free_port() -> int:
        """Ask the kernel for a free TCP port, then hand it to opencode.

        ``--port 0`` prefers opencode's compiled-in default (4096) and
        only falls back to a random port when busy — concurrent sessions
        would race on 4096. An explicit port removes the ambiguity.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    async def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("opencode serve terminate failed", exc_info=True)
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        with suppress(ProcessLookupError):
            proc.kill()

    async def _ensure_server(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("session closed")
        if (
            self._client is not None
            and self._proc is not None
            and self._port is not None
        ):
            return self._client

        # A previous failed attempt may have left a half-started serve
        # behind — reap it before spawning a fresh one (no orphans).
        if self._proc is not None:
            await self._terminate_proc()

        port = self._pick_free_port()
        env = dict(os.environ)
        env.update(self._spec.env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "opencode", "serve", "--port", str(port),
                cwd=self._spec.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception as exc:
            raise _TransportError(
                "opencode_spawn_error",
                f"could not spawn `opencode serve`: {exc}",
            ) from exc

        try:
            self._port = await self._discover_port()
        except Exception:
            await self._terminate_proc()
            raise

        self._client = self._build_client()
        return self._client

    async def _discover_port(self) -> int:
        """Read the serve stdout banner until the port line appears.

        The banner is printed on stdout (stderr stays empty) — reading
        stderr here is what broke port discovery historically.
        """
        assert self._proc is not None and self._proc.stdout is not None
        deadline = asyncio.get_event_loop().time() + _SERVER_READY_TIMEOUT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=_LINE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                continue
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace")
            m = _PORT_RE.search(decoded)
            if m:
                return int(m.group(1))
        raise _TransportError(
            "opencode_port_discovery",
            "could not discover opencode serve port within "
            f"{_SERVER_READY_TIMEOUT_SECONDS:.0f}s "
            "(process may have failed to start)",
        )

    def _build_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            client = self._client_factory(self._port)
        else:
            client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{self._port}",
                timeout=self._http_timeout(),
            )
        return client

    # --- model / spec mapping -------------------------------------------

    def _model_ref(self) -> dict[str, str] | None:
        """Parse ``spec.model`` as ``{providerID}/{modelID}``.

        opencode's ModelRef requires both fields; a bare model id would
        silently select the free default model (rate-limited), so the
        backend refuses to guess and logs a warning instead.
        """
        model = (self._spec.model or "").strip()
        if not model:
            return None
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
            if provider_id and model_id:
                return {"providerID": provider_id, "id": model_id}
        logger.warning(
            "opencode: spec.model %r is not in providerID/modelID form — "
            "using the opencode default model",
            model,
        )
        return None

    # --- opencode HTTP calls ---------------------------------------------

    async def _create_oc_session(self, client: Any) -> str:
        body: dict[str, Any] = {}
        model_ref = self._model_ref()
        if model_ref is not None:
            body["model"] = model_ref
        resp = await client.post(
            "/api/session", json=body, timeout=self._handshake_timeout()
        )
        if resp.status_code != 200:
            raise _TransportError(
                "opencode_session_create",
                f"POST /api/session returned {resp.status_code}",
            )
        data = (resp.json() or {}).get("data") or {}
        sid = data.get("id")
        if not sid:
            raise _TransportError(
                "opencode_session_create",
                "POST /api/session response carried no session id",
            )
        self._oc_session_id = str(sid)
        return self._oc_session_id

    async def _submit_prompt(self, client: Any, sid: str, text: str) -> None:
        resp = await client.post(
            f"/api/session/{sid}/prompt",
            json={"prompt": {"text": text}},
            timeout=self._handshake_timeout(),
        )
        if resp.status_code != 200:
            raise _TransportError(
                "opencode_prompt_rejected",
                f"POST /api/session/{sid}/prompt returned {resp.status_code}",
            )

    async def _wait_model_catalog(self, client: Any) -> None:
        """Wait for the models.dev catalog before the first prompt.

        A fresh serve starts with an EMPTY catalog and backfills it
        asynchronously (~5-15s, observed via ``GET /api/model``). A
        prompt submitted before that dies **silently** with
        ``ModelUnavailableError`` — no bus event, no step frame; only
        the core's first_turn watchdog would ever notice. Poll until the
        requested model (or any model, when unspecified) is resolvable,
        bounded well inside the core's handshake budget.
        """
        ref = self._model_ref()
        provider = ref.get("providerID") if ref else None
        model_id = ref.get("id") if ref else None
        budget = min(self._handshake_timeout(), 15.0)
        deadline = asyncio.get_event_loop().time() + budget
        while True:
            try:
                resp = await client.get("/api/model", timeout=10.0)
                models = (resp.json() or {}).get("data") or []
            except Exception:  # noqa: BLE001 - catalog probe: absence = not ready
                models = []
            ready = bool(models)
            if ready and provider is not None:
                ready = any(
                    m.get("providerID") == provider
                    and (model_id is None or m.get("id") == model_id)
                    for m in models
                    if isinstance(m, dict)
                )
            if ready:
                return
            if asyncio.get_event_loop().time() >= deadline:
                logger.warning(
                    "opencode: model catalog not ready within %.0fs "
                    "(provider=%r model=%r) — submitting prompt anyway",
                    budget, provider, model_id,
                )
                return
            await asyncio.sleep(1.0)

    # --- SSE frame parsing ------------------------------------------------

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | None:
        """Parse a single SSE line into a JSON frame, or ``None``.

        Recognizes ``data: {...}`` (with or without the space); comments
        (``:``), blanks and non-``data`` lines are dropped.
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
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    # --- frame → SPI translation -------------------------------------------

    def _ingest_frame(self, frame: dict[str, Any], sid: str) -> str | None:
        """Translate one bus frame into 0..n SPI envelopes.

        Returns the terminal ``finish`` value when this frame ends the
        turn (a ``step.ended`` whose ``finish`` is not ``tool-calls``);
        otherwise ``None``. Frames belonging to other sessions (shared
        bus) are filtered by ``data.sessionID``.
        """
        f_type = str(frame.get("type", ""))
        data = frame.get("data")
        if not isinstance(data, dict):
            return None
        frame_sid = data.get("sessionID")
        if frame_sid is not None and frame_sid != sid:
            return None  # another session's traffic on the shared bus

        if f_type == "session.next.prompt.admitted":
            # Liveness marker: the core's handshake watchdog needs a
            # first event within handshake_timeout_s, long before the
            # model's first token. Empty text is skipped by the core's
            # output accumulation (``if text:``).
            self._deny_grace_deadline = None
            self._emit(EventKind.TEXT_DELTA, {"text": "", "delta": ""})

        elif f_type == "session.next.text.delta":
            self._deny_grace_deadline = None
            delta = str(data.get("delta", ""))
            text_id = str(data.get("textID", ""))
            if text_id:
                self._text_delta_ids.add(text_id)
            if delta:
                self._emit(
                    EventKind.TEXT_DELTA,
                    {"text": delta, "delta": delta},
                )

        elif f_type == "session.next.reasoning.delta":
            # dsh parity: reasoning streams as TEXT_DELTA — keeps the
            # core's inactivity watchdog fed during long thinking phases.
            self._deny_grace_deadline = None
            delta = str(data.get("delta", ""))
            if delta:
                self._emit(
                    EventKind.TEXT_DELTA,
                    {"text": delta, "delta": delta},
                )

        elif f_type == "session.next.tool.progress":
            # Long tool executions emit progress frames — forward them
            # as liveness so inactivity does not fire mid-tool.
            self._deny_grace_deadline = None
            self._emit(EventKind.TEXT_DELTA, {"text": "", "delta": ""})

        elif f_type == "session.next.text.ended":
            text_id = str(data.get("textID", ""))
            text = str(data.get("text", ""))
            # Deltas already carried the content — re-emitting the full
            # text would double-count in the core's output_text.
            if text and text_id not in self._text_delta_ids:
                self._emit(EventKind.TEXT, {"text": text})

        elif f_type == "session.next.tool.called":
            self._deny_grace_deadline = None
            call_id = str(data.get("callID", ""))
            tool_input = data.get("input")
            if isinstance(tool_input, dict):
                self._tool_inputs[call_id] = tool_input
            self._emit(
                EventKind.TOOL_CALL,
                {
                    "call_id": call_id,
                    "name": str(data.get("tool", "")),
                    "arguments": tool_input if isinstance(tool_input, dict) else {},
                },
            )

        elif f_type == "session.next.tool.input.ended":
            call_id = str(data.get("callID", ""))
            try:
                self._tool_inputs[call_id] = json.loads(str(data.get("text", "")))
            except json.JSONDecodeError:
                pass

        elif f_type in ("session.next.tool.success", "session.next.tool.failed"):
            ok = f_type == "session.next.tool.success"
            content = data.get("content")
            parts: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
            output = "\n".join(parts) if parts else None
            if output is None and isinstance(data.get("structured"), dict):
                # read/glob/grep carry their payload in
                # ``structured.content`` with an EMPTY ``content`` list
                # (verified live) — fall back rather than dropping the
                # result from the transcript.
                structured = data["structured"]
                structured_content = structured.get("content")
                output = (
                    str(structured_content)
                    if structured_content is not None
                    else json.dumps(structured, ensure_ascii=False)[:500]
                )
            if output is None and not ok:
                # tool.failed carries its reason under ``error.message``.
                error = data.get("error")
                output = (
                    str(error.get("message", ""))
                    if isinstance(error, dict)
                    else None
                )
            self._emit(
                EventKind.TOOL_RESULT,
                {
                    "call_id": str(data.get("callID", "")),
                    "ok": ok,
                    "output": output,
                },
            )

        elif f_type == "session.next.step.failed":
            error = data.get("error")
            message = (
                str(error.get("message", ""))
                if isinstance(error, dict)
                else "step failed"
            )
            self._last_error_message = message
            # Not terminal: opencode may retry the step (session.next.retried)
            # and continue the turn.
            self._emit(
                EventKind.ERROR,
                {"code": "opencode_step_failed", "message": message},
            )

        elif f_type == "permission.v2.asked":
            request_id = str(data.get("id", ""))
            source = data.get("source") or {}
            call_id = str(source.get("callID", "")) if isinstance(source, dict) else ""
            tool_name = str(data.get("action", ""))
            arguments = self._tool_inputs.get(call_id)
            if arguments is None:
                resources = data.get("resources")
                arguments = (
                    {"resources": resources}
                    if isinstance(resources, list)
                    else {}
                )
            if request_id:
                self._asked_tools[request_id] = tool_name
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
                    },
                )

        elif f_type == "permission.v2.replied":
            # Deny watch arming: opencode abandons the turn after a
            # rejection (tool fails, then eternal silence) — arm the
            # grace window; the pump synthesizes the terminal when it
            # expires. Any non-reject reply disarms it.
            if str(data.get("reply", "")) == "reject":
                self._deny_grace_deadline = time.monotonic() + _DENY_GRACE_SECONDS
                self._deny_tool_name = self._asked_tools.get(
                    str(data.get("requestID", "")), "unknown"
                )
            else:
                self._deny_grace_deadline = None

        elif f_type == "session.next.step.started":
            self._deny_grace_deadline = None

        elif f_type == "session.next.step.ended":
            self._accumulate_usage(data.get("tokens"))
            finish = str(data.get("finish", "stop"))
            if finish == "tool-calls":
                return None  # turn continues with the next step
            return finish

        else:
            # Preserve forward-compatible provider events instead of
            # dropping them — renderers show these as collapsed raw
            # events (parity with the claude/codex/clawcodex backends).
            self._emit(
                EventKind.UNKNOWN,
                {"event": f_type, "raw": dict(frame)},
            )

        return None

    def _accumulate_usage(self, tokens: Any) -> None:
        if not isinstance(tokens, dict):
            return
        for key in ("input", "output", "reasoning"):
            value = tokens.get(key)
            if isinstance(value, (int, float)):
                self._usage_totals[key] = self._usage_totals.get(key, 0) + int(value)
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for key in ("read", "write"):
                value = cache.get(key)
                if isinstance(value, (int, float)):
                    flat = f"cache_{key}"
                    self._usage_totals[flat] = (
                        self._usage_totals.get(flat, 0) + int(value)
                    )

    # --- turn body ---------------------------------------------------------

    async def _pump_bus(
        self,
        client: Any,
        sid: str,
        prompt_task: asyncio.Task,
    ) -> str:
        """Consume the global bus until this session's turn terminates.

        The bus subscription is opened **before** the prompt is
        submitted (via the already-started ``prompt_task`` racing the
        frame loop) so no early frame — including the terminal one from
        an instant answer — can be missed.
        """
        async with client.stream(
            "GET",
            "/api/event",
            headers={"Accept": "text/event-stream"},
            timeout=self._http_timeout(),
        ) as resp:
            if resp.status_code != 200:
                raise _TransportError(
                    "opencode_bus_open",
                    f"GET /api/event returned {resp.status_code}",
                )
            if not _is_sse_response(resp):
                raise _TransportError(
                    "opencode_bus_not_sse",
                    "GET /api/event did not return an event stream "
                    f"(content-type={(resp.headers or {}).get('content-type', '')!r})",
                )
            aiter = resp.aiter_lines()
            next_line = asyncio.ensure_future(anext(aiter))
            finish: str | None = None
            deny_timer: asyncio.Task | None = None
            pending: set[asyncio.Task] = {next_line, prompt_task}
            if self._deny_grace_deadline is not None:
                deny_timer = asyncio.ensure_future(
                    asyncio.sleep(
                        max(0.0, self._deny_grace_deadline - time.monotonic())
                    )
                )
                pending.add(deny_timer)
            try:
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    if deny_timer is not None and deny_timer in done:
                        # Deny grace expired: opencode abandoned the turn
                        # after the rejection (verified live) — synthesize
                        # the honest terminal instead of burning the
                        # core's inactivity budget.
                        self._deny_grace_deadline = None
                        deny_timer = None
                        message = (
                            f"approval denied for tool "
                            f"'{self._deny_tool_name}': opencode abandoned "
                            "the turn after the rejection (no step "
                            "completion — synthesized by the backend)"
                        )
                        self._last_error_message = message
                        self._emit(
                            EventKind.ERROR,
                            {"code": "opencode_approval_denied", "message": message},
                        )
                        finish = "denied"
                        break
                    if prompt_task in done:
                        # Re-raises a prompt rejection (400/404/409…).
                        prompt_task.result()
                    if next_line in done:
                        try:
                            line = next_line.result()
                        except StopAsyncIteration:
                            break
                        next_line = asyncio.ensure_future(anext(aiter))
                        pending.add(next_line)
                        payload = self._parse_sse_line(line)
                        if payload is None:
                            continue
                        frame_finish = self._ingest_frame(payload, sid)
                        if frame_finish is not None:
                            finish = frame_finish
                            break
                    # Maintain the deny watch across the wait set: cleared
                    # by model-level activity, kept (absolute deadline) by
                    # the expected post-rejection tool frames.
                    if deny_timer is not None:
                        if self._deny_grace_deadline is None:
                            deny_timer.cancel()
                            pending.discard(deny_timer)
                            deny_timer = None
                        elif deny_timer in pending and deny_timer.done():
                            pending.discard(deny_timer)
                            deny_timer = asyncio.ensure_future(
                                asyncio.sleep(
                                    max(
                                        0.0,
                                        self._deny_grace_deadline
                                        - time.monotonic(),
                                    )
                                )
                            )
                            pending.add(deny_timer)
                    elif self._deny_grace_deadline is not None:
                        deny_timer = asyncio.ensure_future(
                            asyncio.sleep(
                                max(
                                    0.0,
                                    self._deny_grace_deadline - time.monotonic(),
                                )
                            )
                        )
                        pending.add(deny_timer)
            finally:
                for task in (next_line, deny_timer):
                    if task is not None and not task.done():
                        task.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await task
            if prompt_task in pending and not prompt_task.done():
                with suppress(Exception):
                    await asyncio.wait_for(prompt_task, timeout=30.0)
            if finish is None:
                raise _TransportError(
                    "opencode_turn_unterminated",
                    "event stream ended without a terminal step",
                )
            return finish

    async def _run_turn(self, text: str) -> None:
        """Blocking turn body — runs as a background task.

        Every failure mode becomes an ERROR envelope (never a silent
        success); the turn task itself never raises.
        """
        error_emitted = False
        try:
            client = await self._ensure_server()
            first_turn = self._oc_session_id is None
            if first_turn:
                await self._create_oc_session(client)
                # Fresh serve → empty models.dev catalog → a premature
                # prompt dies silently. Wait for resolvability.
                await self._wait_model_catalog(client)
            assert self._oc_session_id is not None
            sid = self._oc_session_id

            self._text_delta_ids.clear()
            self._deny_grace_deadline = None  # fresh turn, fresh watch
            prompt_task = asyncio.create_task(
                self._submit_prompt(client, sid, text)
            )
            try:
                finish = await self._pump_bus(client, sid, prompt_task)
            except BaseException:
                # Re-raise the pump failure, but never let the prompt
                # task's own CancelledError mask it (CancelledError is a
                # BaseException — suppress it explicitly or the original
                # transport error is replaced and the turn reports a
                # bogus success).
                if not prompt_task.done():
                    prompt_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await prompt_task
                raise

            if finish == "denied":
                # The pump already emitted the structured
                # opencode_approval_denied ERROR — the run must fail
                # honestly instead of reporting success.
                error_emitted = True
            self._emit(
                EventKind.TURN_COMPLETE,
                {
                    "reason": "success" if finish == "stop" else str(finish),
                    "finish": str(finish),
                },
            )
        except Exception as exc:  # noqa: BLE001 - SPI boundary: any failure must surface as an ERROR event
            code = getattr(exc, "code", "opencode_error")
            message = f"{type(exc).__name__}: {exc}"
            self._last_error_message = message
            logger.warning("opencode turn failed: %s", message)
            self._emit(EventKind.ERROR, {"code": code, "message": message})
            error_emitted = True
        finally:
            payload: dict[str, Any] = {
                "reason": "error" if error_emitted else "success"
            }
            if self._usage_totals:
                payload["usage"] = dict(self._usage_totals)
            if error_emitted and self._last_error_message:
                payload["message"] = self._last_error_message
            self._emit(EventKind.SESSION_COMPLETE, payload)

    # --- SPI send / events ---------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        """Non-blocking: spawn the turn task and return immediately.

        Events flow through the queue into ``events()`` while the turn
        runs — required for streaming metrics, five-level timeouts and
        the approval round-trip (the core consumes events concurrently).
        """
        if self._closed:
            raise RuntimeError("session closed")
        # Serialize turns: a follow-up send waits for the previous turn
        # task (including its terminal SESSION_COMPLETE).
        if self._turn_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await self._turn_task
        text = content if isinstance(content, str) else str(content)
        self._turn_started = True
        self._turn_task = asyncio.create_task(self._run_turn(text))

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[EventEnvelope]:
        if not self._turn_started:
            # Contract: events() before any send() is an exhausted
            # stream, not a hanging one (dsh parity).
            return
        while True:
            envelope = await self._queue.get()
            yield envelope
            if envelope.kind is EventKind.SESSION_COMPLETE:
                break

    # --- SPI interrupt / approve / probe / close ------------------------

    async def interrupt(self) -> None:
        if self._oc_session_id is None or self._client is None:
            return
        try:
            await self._client.post(
                f"/api/session/{self._oc_session_id}/interrupt",
                timeout=self._handshake_timeout(),
            )
        except Exception as exc:  # noqa: BLE001 - SPI boundary: interrupt is best-effort
            logger.warning("opencode interrupt failed: %s", exc)

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        req = self._pending_approvals.pop(request_id, None)
        if req is None:
            logger.warning("approve() called for unknown request_id=%s", request_id)
            return
        if self._client is None or self._oc_session_id is None:
            logger.warning("approve() called before the server is ready")
            return
        reply = {"allow": "once", "always_allow": "always"}.get(
            decision.value, "reject"
        )
        try:
            resp = await self._client.post(
                f"/api/session/{self._oc_session_id}/permission/{request_id}/reply",
                json={"reply": reply},
                timeout=self._handshake_timeout(),
            )
            if resp.status_code not in (200, 204):
                logger.warning(
                    "opencode permission reply returned %s for %s",
                    resp.status_code,
                    request_id,
                )
        except Exception as exc:  # noqa: BLE001 - SPI boundary: approval reply is best-effort
            logger.warning("opencode permission reply failed for %s: %s", request_id, exc)

    async def probe_resume(self) -> ResumeStatus:
        """Probe via ``GET /api/session/{id}`` (404 = transcript gone).

        Non-opencode ids (the orchestrator's run/stage ids are not
        ``ses_*``) cannot be probed at all — honest UNDETECTABLE rather
        than a misleading REJECTED terminal.
        """
        rid = self._spec.resume_session_id
        if not rid:
            # Fresh session — nothing to probe (dsh parity: RESUMED).
            return ResumeStatus.RESUMED
        if not _OC_SESSION_ID_RE.match(rid):
            return ResumeStatus.UNDETECTABLE
        # Use the pre-injected client when present (a probe must not
        # spawn a server just to answer); otherwise bootstrap one.
        client = self._client
        if client is None:
            try:
                client = await self._ensure_server()
            except Exception as exc:  # noqa: BLE001 - probe: any bootstrap failure = UNDETECTABLE
                logger.warning(
                    "OpenCodeSession.probe_resume: server not ready: %s", exc
                )
                return ResumeStatus.UNDETECTABLE
        try:
            resp = await client.get(
                f"/api/session/{rid}", timeout=self._handshake_timeout()
            )
            if resp.status_code == 200:
                return ResumeStatus.RESUMED
            if resp.status_code == 404:
                return ResumeStatus.REJECTED
            return ResumeStatus.UNDETECTABLE
        except Exception as exc:  # noqa: BLE001 - probe: any HTTP failure = UNDETECTABLE
            logger.warning("OpenCodeSession.probe_resume failed: %s", exc)
            return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            # CancelledError must be suppressed explicitly — awaiting a
            # cancelled task re-raises it here, and letting it escape
            # would break the core's cleanup on operator stop.
            with suppress(asyncio.CancelledError, Exception):
                await self._turn_task
        await self._terminate_proc()
        if self._client is not None:
            with suppress(Exception):
                await self._client.aclose()
            self._client = None
        self._pending_approvals.clear()

    def close_sync(self) -> None:
        """Synchronous teardown for backend dispose (no running loop)."""
        self._closed = True
        proc = self._proc
        self._proc = None
        if proc is not None:
            with suppress(Exception):
                proc.terminate()
            with suppress(Exception):
                proc.wait(timeout=5.0)
            with suppress(Exception):
                proc.kill()
        self._pending_approvals.clear()

