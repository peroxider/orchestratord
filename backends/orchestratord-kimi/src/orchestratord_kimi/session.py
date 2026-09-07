"""KimiSession — ACP (Agent Client Protocol) translation for ``kimi acp``.

Real wire format (ported from the multica Go reference,
``server/pkg/agent/kimi.go`` and the shared ``hermesClient`` transport):
Kimi Code CLI is driven over ACP JSON-RPC 2.0 on stdin/stdout — one
NDJSON frame per line.  The Go daemon spawns ``kimi acp`` per task and
drives the lifecycle::

    initialize → session/new (or session/resume) → [session/set_model]
    → session/prompt → drain trailing notifications → close stdin, reap

Kimi streams interim narration and the final answer as the same
``session/update agent_message_chunk`` type (→ TEXT_DELTA), tool
activity as ``tool_call`` / ``tool_call_update`` (→ TOOL_CALL /
TOOL_RESULT), and finishes the turn with the ``session/prompt``
response ``stopReason`` (→ TURN_COMPLETE / SESSION_COMPLETE).
``session/request_permission`` is auto-answered in-protocol with the
safest offered option (Go ``selectACPPermissionOption``) — there is no
human approval round-trip, so ``approval_hooks`` stays False.  stderr
is sniffed for terminal provider errors (HTTP 4xx / auth / rate-limit)
and promotes an otherwise successful turn to ERROR (Go
``promoteACPResultOnProviderError``).

Kimi streams tool args token-by-token across ``tool_call_update``
frames, so the ``tool_call`` start frame is buffered and the deferred
TOOL_CALL is emitted at completion with the parsed args (Go
``emitDeferredToolUse``).

Structural conventions follow the previous stub (spawn in ``send()``,
buffered async ``events()``, per-turn timeout, ``_next_seq``); the CI
drift guard keeps ``descriptor.capabilities`` in sync with
``KimiBackend.capabilities()``.

Known limitations vs the Go reference (FEATURE_GAP §8.2.3):
* token usage is not extracted — kimi-code exports no usage over ACP
  and the Go daemon falls back to scanning the ``KIMI_CODE_HOME`` wire
  log, which has no SPI event surface here;
* the stderr sniffer drops Go's structured-log echo suppression (it
  matches the same terminal-error markers on plain lines);
* MCP ``mcp_config`` translation is not plumbed (SessionSpec carries no
  MCP config) — ``session/new``/``session/resume`` send an empty
  ``mcpServers`` array.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

logger = logging.getLogger(__name__)

_DEFAULT_HANDSHAKE_TIMEOUT_S = 30.0
_DEFAULT_TOTAL_TIMEOUT_S = 600.0
# hermes.go: acpNotificationQuietTime (250ms) and the per-backend reader
# drain grace (kimiReaderDrainGrace = 2s).  Both are tunable per session
# via ``SessionSpec.extra["acp_quiet_s"]`` / ``["acp_drain_grace_s"]``.
_ACP_QUIET_S = 0.25
_READER_DRAIN_GRACE_S = 2.0

_STOP_REASON_CANCELLED = "cancelled"

# hermes.go acpProviderErrorSniffer markers (kimi-style subset).  The
# full Go sniffer also tracks structured-log echo JSON; this port scans
# plain lines only.
_ACP_ERROR_HEADER_RE = re.compile(
    r"(?:⚠️|❌|\[ERROR\]).*(?:BadRequestError|AuthenticationError"
    r"|RateLimitError|HTTP [0-9]{3}|Non-retryable|API call failed)"
)
_ACP_TERMINAL_ERROR_RE = re.compile(
    r"(?:❌|\[ERROR\]|after \d+ retr|Non-retryable|BadRequestError"
    r"|AuthenticationError)"
)
_KIMI_PROVIDER_API_ERROR_RE = re.compile(r"provider\.api_error")
_KIMI_TERMINAL_ERROR_RE = re.compile(r"provider\.api_error: (?:400|401|403)")
_MAX_CAPTURED_ERROR_LINES = 8


class KimiAcpError(RuntimeError):
    """The kimi ACP agent failed a lifecycle request or the transport."""


# -- wire-format helpers (ported from hermes.go / kimi.go) ---------------


def kimi_tool_name_from_title(title: str) -> str:
    """Normalise an ACP ``tool_call`` title into a snake_case identifier.

    Port of ``kimiToolNameFromTitle``: ACP titles look like
    ``"Read file: /path/to/foo.go"`` or ``"Run command: ls"`` — strip
    everything after the first colon, map the known kimi labels onto the
    canonical tool ids, and snake_case anything else.
    """
    t = title.strip()
    if not t:
        return ""
    idx = t.find(":")
    if idx > 0:
        t = t[:idx].strip()
    lower = t.lower()
    mapped = _TOOL_NAME_BY_TITLE.get(lower)
    if mapped is not None:
        return mapped
    return lower.replace(" ", "_")


_TOOL_NAME_BY_TITLE = {
    "read": "read_file",
    "read file": "read_file",
    "write": "write_file",
    "write file": "write_file",
    "edit": "edit_file",
    "patch": "edit_file",
    # hermes-style lowercase title; the Go reference reaches this result
    # by composing hermesToolNameFromTitle with kimiToolNameFromTitle.
    "patch (replace)": "edit_file",
    "shell": "terminal",
    "bash": "terminal",
    "terminal": "terminal",
    "run command": "terminal",
    "run shell command": "terminal",
    "search": "search_files",
    "grep": "search_files",
    "find": "search_files",
    "glob": "glob",
    "web search": "web_search",
    "fetch": "web_fetch",
    "web fetch": "web_fetch",
    "todo": "todo_write",
    "todo write": "todo_write",
}

# hermes.go acpSessionScopedOptionIDs — session-scoped grant ids that
# carry ACP kind "allow_always" but must not outlive the task.
_SESSION_SCOPED_OPTION_IDS = ("allow_session", "approve_for_session")


def _is_grant_kind(kind: str) -> bool:
    """ACP PermissionOptionKind grants only via the two allow kinds."""
    return str(kind).strip().lower() in ("allow_once", "allow_always")


def select_kimi_permission_option(
    params: dict[str, Any],
) -> tuple[str, bool] | None:
    """Auto-answer selector ported from ``selectACPPermissionOption``.

    Returns ``(optionId, grants)`` for the safest offered option, or
    ``None`` when nothing is safely selectable (the caller then replies
    with a JSON-RPC protocol error instead of fabricating an outcome).
    Preference: session-scoped grant → ``allow_once`` → ``reject_once``.
    A permanent ``allow_always`` grant is never auto-selected.
    """
    options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
    for want in _SESSION_SCOPED_OPTION_IDS:
        for opt in options:
            if opt.get("optionId") == want and _is_grant_kind(
                str(opt.get("kind", ""))
            ):
                return str(opt["optionId"]), True
    for opt in options:
        option_id = str(opt.get("optionId") or "")
        if option_id and str(opt.get("kind", "")).strip().lower() == "allow_once":
            return option_id, True
    for opt in options:
        option_id = str(opt.get("optionId") or "")
        if option_id and str(opt.get("kind", "")).strip().lower() == "reject_once":
            return option_id, False
    return None


def _normalize_update_type(raw: str) -> str:
    """Port of ``normalizeACPUpdateType`` (separators are insignificant)."""
    key = raw.strip().replace("_", "").replace("-", "").lower()
    if key == "agentmessagechunk":
        return "agent_message_chunk"
    if key == "agentthoughtchunk":
        return "agent_thought_chunk"
    if key == "toolcall":
        return "tool_call"
    if key == "toolcallupdate":
        return "tool_call_update"
    if key == "usageupdate":
        return "usage_update"
    if key in ("turnend", "endturn"):
        return "turn_end"
    return ""


def _normalize_update(update: Any) -> tuple[str, dict[str, Any]]:
    """Port of ``normalizeACPUpdate``: three accepted serializations."""
    if not isinstance(update, dict):
        return "", {}
    kind = str(update.get("sessionUpdate") or update.get("type") or "")
    if kind:
        return _normalize_update_type(kind), update
    # Externally tagged variant: {"agentMessageChunk": {"content": ...}}.
    if len(update) == 1:
        ((key, value),) = update.items()
        if isinstance(value, dict):
            return _normalize_update_type(key), value
    return "", update


def _content_text(content: Any) -> str:
    """Join the text of ACP content blocks (``extractACPToolCallText``)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _parse_tool_args_json(args_text: str) -> dict[str, Any]:
    """Port of ``parseToolArgsJSON`` — kimi accumulates args as JSON."""
    args_text = args_text.strip()
    if not args_text:
        return {}
    try:
        parsed = json.loads(args_text)
    except json.JSONDecodeError:
        return {"text": args_text}
    return parsed if isinstance(parsed, dict) else {"text": args_text}


def _acp_raw_text(raw: Any) -> str:
    """Unwrap an ACP rawOutput/output field into plain text."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("output", "text"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
    return ""


@dataclass
class _PendingTool:
    """Buffered ``tool_call`` start frame awaiting its args stream."""

    tool_name: str
    args_text: str = ""
    emitted: bool = False


class _ProviderErrorSniffer:
    """Detect terminal upstream-LLM failures on the agent's stderr.

    Faithful subset of hermes.go ``acpProviderErrorSniffer``: capture
    deduped provider-error header lines and flag the terminal ones
    (❌/[ERROR]/non-retryable/4xx-auth, plus kimi's
    ``provider.api_error: 400|401|403``).
    """

    def __init__(self, *, kimi_style: bool) -> None:
        self._kimi_style = kimi_style
        self._seen: set[str] = set()
        self.lines: list[str] = []
        self.terminal = False

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        is_kimi_err = self._kimi_style and bool(
            _KIMI_PROVIDER_API_ERROR_RE.search(line)
        )
        if not (
            _ACP_ERROR_HEADER_RE.search(line)
            or _ACP_TERMINAL_ERROR_RE.search(line)
            or is_kimi_err
        ):
            return
        if _ACP_TERMINAL_ERROR_RE.search(line) or (
            self._kimi_style and _KIMI_TERMINAL_ERROR_RE.search(line)
        ):
            self.terminal = True
        if line in self._seen:
            return
        self._seen.add(line)
        self.lines.append(line)
        if len(self.lines) > _MAX_CAPTURED_ERROR_LINES:
            del self.lines[: len(self.lines) - _MAX_CAPTURED_ERROR_LINES]

    @property
    def summary(self) -> str:
        return "\n".join(self.lines)


# -- session --------------------------------------------------------------


class KimiSession:
    """Adapt the ``kimi acp`` ACP subprocess into the AgentSession SPI."""

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"kimi-{id(self)}"
        self.conversation_id: str | None = None
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,   # session/update agent_message_chunk
            resumable=False,         # continuity via session/resume per turn
            interrupt=False,         # best-effort session/cancel only
            approval_hooks=False,    # permissions auto-answered in-protocol
            parallel_sessions=True,  # one process per turn
            cost_reporting=False,    # no usage over ACP (wire-log scan in Go)
            tool_filtering=False,
            takeover=False,
            goal_mode=False,
            resume_detection=False,
        )
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._reply_tasks: set[asyncio.Task[None]] = set()
        self._native_session_id: str | None = None
        self._pending_tools: dict[str, _PendingTool] = {}
        self._stderr = _ProviderErrorSniffer(kimi_style=True)
        self._last_activity = 0.0
        extra = spec.extra or {}
        self._quiet_s = float(extra.get("acp_quiet_s", _ACP_QUIET_S))
        self._drain_grace_s = float(
            extra.get("acp_drain_grace_s", _READER_DRAIN_GRACE_S)
        )

    # -- seq / timestamp helpers (stub convention) -----------------------

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

    # -- subprocess + lifecycle ------------------------------------------

    def _binary(self) -> str:
        """Resolve the kimi binary (multica-style ``KIMI_PATH`` override)."""
        return self._spec.runtime_bin or os.environ.get("KIMI_PATH") or "kimi"

    def _argv(self) -> list[str]:
        # kimi.go: the `acp` subcommand drives the ACP JSON-RPC transport
        # and is hardcoded (blocked from user custom args).
        return [self._binary(), "acp"]

    def _build_env(self) -> dict[str, str] | None:
        # Go buildEnv inherits the parent environment and overlays cfg.Env.
        if not self._spec.env:
            return None
        env = dict(os.environ)
        env.update(self._spec.env)
        return env

    async def _spawn(self) -> None:
        cmd = self._argv()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._spec.cwd or None,
                env=self._build_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise KimiAcpError(f"failed to spawn kimi {cmd!r}: {exc}") from exc
        self._last_activity = asyncio.get_running_loop().time()
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="kimi-acp-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name="kimi-acp-stderr"
        )

    async def _ensure_started(self) -> None:
        """Spawn the per-turn process and run the ACP initialize handshake.

        kimi.go step 1: protocolVersion 1, multica-agent-sdk client info,
        terminal client capability.
        """
        if self._proc is not None:
            return
        await self._spawn()
        timeout = self._spec.handshake_timeout_s or _DEFAULT_HANDSHAKE_TIMEOUT_S
        resp = await self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "multica-agent-sdk", "version": "0.2.0"},
                "clientCapabilities": {"terminal": True},
            },
            timeout_s=timeout,
        )
        if "error" in resp:
            err = resp["error"]
            raise KimiAcpError(
                f"kimi initialize failed: {err.get('message', err)}"
            )

    async def _open_session(self) -> None:
        """session/new for a fresh conversation, session/resume otherwise."""
        cwd = self._spec.cwd or "."
        if self._native_session_id is None and not self._spec.resume_session_id:
            resp = await self._request(
                "session/new",
                {"cwd": cwd, "mcpServers": []},
                timeout_s=self._spec.handshake_timeout_s
                or _DEFAULT_HANDSHAKE_TIMEOUT_S,
            )
            if "error" in resp:
                err = resp["error"]
                raise KimiAcpError(f"kimi session/new failed: {err.get('message', err)}")
            session_id = str((resp.get("result") or {}).get("sessionId") or "")
            if not session_id:
                raise KimiAcpError("kimi session/new returned no session ID")
        else:
            requested = self._native_session_id or self._spec.resume_session_id
            assert requested is not None
            resp = await self._request(
                "session/resume",
                {"cwd": cwd, "sessionId": requested, "mcpServers": []},
                timeout_s=self._spec.handshake_timeout_s
                or _DEFAULT_HANDSHAKE_TIMEOUT_S,
            )
            if "error" in resp:
                err = resp["error"]
                raise KimiAcpError(
                    f"kimi session/resume failed: {err.get('message', err)}"
                )
            # resolveResumedSessionID: keep the echoed id when the agent
            # returns one, otherwise continue with the requested id.
            session_id = str(
                (resp.get("result") or {}).get("sessionId") or requested
            )
        self._native_session_id = session_id

    async def _set_model(self) -> None:
        """kimi.go step 3: session/set_model MUST fail the turn on error —
        silently falling back would run the turn on a different model than
        the user picked."""
        assert self._native_session_id is not None
        resp = await self._request(
            "session/set_model",
            {"sessionId": self._native_session_id, "modelId": self._spec.model},
            timeout_s=self._spec.handshake_timeout_s
            or _DEFAULT_HANDSHAKE_TIMEOUT_S,
        )
        if "error" in resp:
            err = resp["error"]
            raise KimiAcpError(
                f"kimi could not switch to model {self._spec.model!r}: "
                f"{err.get('message', err)}"
            )

    # -- SPI send / events -----------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        text = content if isinstance(content, str) else str(content)
        reason = "success"
        try:
            await self._ensure_started()
            await self._open_session()
            if self._spec.model:
                await self._set_model()
            # kimi.go step 4: prepend the system prompt with the "---"
            # separator (Reasonix is the backend that must NOT get this).
            user_text = text
            if self._spec.system_prompt:
                user_text = f"{self._spec.system_prompt}\n\n---\n\n{text}"
            resp = await self._request(
                "session/prompt",
                {
                    "sessionId": self._native_session_id,
                    "prompt": [{"type": "text", "text": user_text}],
                },
                timeout_s=self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S,
            )
            if "error" in resp:
                reason = "error"
                err = resp["error"]
                self._emit(
                    EventKind.ERROR,
                    {
                        "code": "kimi_prompt_error",
                        "message": str(err.get("message", err)),
                    },
                )
            else:
                stop_reason = str(
                    (resp.get("result") or {}).get("stopReason") or "end_turn"
                )
                # kimi.go: only stopReason=cancelled is special; every
                # other reason (end_turn included) completes the turn.
                if stop_reason == _STOP_REASON_CANCELLED:
                    reason = "aborted"
            # kimi.go: waitForACPNotificationQuiescence — let trailing
            # notifications land before closing the turn so they stay
            # ordered before TURN_COMPLETE.
            await self._await_notification_quiescence()
        except asyncio.TimeoutError:
            reason = "timeout"
            timeout = self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S
            self._emit(
                EventKind.ERROR,
                {
                    "code": "kimi_timeout",
                    "message": f"kimi exceeded {timeout:.1f}s",
                },
            )
        except KimiAcpError as exc:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": "kimi_acp_error", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {
                    "code": "kimi_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            self._pending_tools.clear()

        # kimi.go promoteACPResultOnProviderError: a completed turn whose
        # stderr shows a terminal provider failure is a failure.
        if reason == "success" and self._stderr.terminal:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {
                    "code": "kimi_provider_error",
                    "message": self._stderr.summary,
                },
            )

        self._emit(EventKind.TURN_COMPLETE, {"reason": reason})
        self._emit(EventKind.SESSION_COMPLETE, {"reason": reason})
        # Go closes stdin and reaps the process at the end of every
        # Execute — this session is spawn-per-turn.
        await self._terminate()

    async def _emit_events(self) -> AsyncIterator[EventEnvelope]:
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    async def _await_notification_quiescence(self) -> None:
        """Wait until the agent has been quiet for ``_quiet_s`` (bounded by
        the reader drain grace), so post-response notifications land while
        the turn is still open."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._drain_grace_s
        while True:
            now = loop.time()
            if now - self._last_activity >= self._quiet_s:
                return
            if now >= deadline:
                return
            await asyncio.sleep(0.02)

    # -- interrupt / approve / probe / close ------------------------------

    async def interrupt(self) -> None:
        """Best-effort ACP ``session/cancel`` (capability bit stays False:
        the Go daemon cancels by killing the per-task process instead)."""
        if self._proc is None or self._native_session_id is None:
            return
        try:
            await self._send_notification(
                "session/cancel", {"sessionId": self._native_session_id}
            )
        except KimiAcpError:
            logger.warning("kimi session/cancel delivery failed", exc_info=True)

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        # Permissions are auto-answered in-protocol (see
        # select_kimi_permission_option); there is no pending human
        # approval to resolve — mirrors the unattended Go daemon.
        logger.debug(
            "kimi approve() is a no-op (permissions auto-answered); "
            "request_id=%s decision=%s",
            request_id,
            decision,
        )

    async def probe_resume(self) -> ResumeStatus:
        """kimi has no cross-process resume probe (resume_detection=False
        contract: UNDETECTABLE, then send() attempts session/resume)."""
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
        await self._terminate()

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._reader_task = None
        self._stderr_task = None
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
        self._fail_pending(KimiAcpError("kimi session closed"))

    # -- JSON-RPC transport ------------------------------------------------

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise KimiAcpError("kimi agent not started")
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        # Register the future before the write so a fast response cannot
        # arrive before it is awaited (no lock here — _write_line takes
        # _write_lock itself and asyncio.Lock is not reentrant).
        await self._write_line(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
            proc,
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
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

    async def _write_line(
        self, msg: dict[str, Any], proc: asyncio.subprocess.Process
    ) -> None:
        assert proc.stdin is not None
        async with self._write_lock:
            try:
                proc.stdin.write(json.dumps(msg).encode("utf-8") + b"\n")
                await proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:
                raise KimiAcpError(f"kimi stdin write failed: {exc}") from exc

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break  # EOF — agent exited (Go: closeAllPending)
                try:
                    msg = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.warning("kimi sent non-JSON line: %.200r", raw)
                    continue
                if isinstance(msg, dict):
                    self._last_activity = asyncio.get_running_loop().time()
                    self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("kimi ACP reader loop crashed")
        finally:
            self._fail_pending(
                KimiAcpError(
                    f"kimi process exited (returncode={proc.returncode})"
                )
            )

    async def _stderr_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                self._stderr.feed_line(raw.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("kimi stderr pump stopped", exc_info=True)

    # -- inbound dispatch ---------------------------------------------------

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
                KimiAcpError(f"malformed JSON-RPC response: {msg!r:.200}")
            )

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        params = msg.get("params") or {}
        if msg.get("method") not in ("session/update", "session/notification"):
            return
        update_type, update = _normalize_update(params.get("update"))
        if not update_type:
            return
        if update_type == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if not text:
                return  # hermes.go handleAgentMessage drops empty chunks
            self._emit(EventKind.TEXT_DELTA, {"text": text, "delta": text})
        elif update_type == "agent_thought_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else ""
            self._emit(
                EventKind.UNKNOWN,
                {"event": "agent_thought_chunk", "text": str(text or "")},
            )
        elif update_type == "tool_call":
            self._handle_tool_call_start(update)
        elif update_type == "tool_call_update":
            self._handle_tool_call_update(update)
        # usage_update / turn_end: usage has no SPI surface (kimi exports
        # none over ACP anyway) and turn completion derives from the
        # session/prompt response, as in kimi.go.

    def _handle_tool_call_start(self, update: dict[str, Any]) -> None:
        call_id = str(update.get("toolCallId") or "")
        title = str(update.get("title") or update.get("name") or "")
        tool_name = kimi_tool_name_from_title(title)
        raw_input = (
            update.get("rawInput")
            or update.get("input")
            or update.get("parameters")
        )
        if raw_input:
            # rawInput present on the start frame: emit immediately.
            self._pending_tools[call_id] = _PendingTool(
                tool_name, emitted=True
            )
            self._emit(
                EventKind.TOOL_CALL,
                {
                    "call_id": call_id,
                    "name": tool_name,
                    "arguments": raw_input,
                },
            )
            return
        # kimi streams args token-by-token across tool_call_update frames;
        # buffer and defer the TOOL_CALL to completion (hermes.go).
        self._pending_tools[call_id] = _PendingTool(
            tool_name, args_text=_content_text(update.get("content"))
        )

    def _handle_tool_call_update(self, update: dict[str, Any]) -> None:
        call_id = str(update.get("toolCallId") or "")
        status = str(update.get("status") or "")
        if status not in ("completed", "failed"):
            # Mid-stream: kimi sends the cumulative args JSON each frame.
            pending = self._pending_tools.get(call_id)
            if pending is not None and not pending.emitted:
                text = _content_text(update.get("content"))
                if text:
                    pending.args_text = text
            return

        pending = self._pending_tools.pop(call_id, None)
        tool_name = ""
        arguments: dict[str, Any] | None = None
        emit_use = False
        if pending is not None and pending.emitted:
            # TOOL_CALL already emitted on the start frame (hermes.go
            # emitDeferredToolUse returns early) — only the result lands.
            emit_use = False
        elif pending is not None:
            tool_name = pending.tool_name
            # A rawInput on the completion frame wins over the buffered
            # content text (hermes.go emitDeferredToolUse).
            raw_input = (
                update.get("rawInput")
                or update.get("input")
                or update.get("parameters")
            )
            arguments = (
                raw_input
                if raw_input
                else _parse_tool_args_json(pending.args_text)
            )
            emit_use = True
        else:
            # Completion arrived without a start frame — synthesize.
            title = str(update.get("title") or update.get("name") or "")
            tool_name = kimi_tool_name_from_title(title)
            arguments = (
                update.get("rawInput")
                or update.get("input")
                or update.get("parameters")
                or {}
            )
            emit_use = True
        if emit_use:
            self._emit(
                EventKind.TOOL_CALL,
                {
                    "call_id": call_id,
                    "name": tool_name,
                    "arguments": arguments if arguments is not None else {},
                },
            )
        output = (
            _acp_raw_text(update.get("rawOutput"))
            or _acp_raw_text(update.get("output"))
            or _content_text(update.get("content"))
        )
        self._emit(
            EventKind.TOOL_RESULT,
            {"call_id": call_id, "ok": status == "completed", "output": output},
        )

    def _handle_incoming_request(self, msg: dict[str, Any]) -> None:
        """Agent→client request: auto-answer permission, refuse the rest.

        hermes.go replies "method not found" (-32601) to unknown agent
        requests so the agent never blocks waiting on us.
        """
        method = str(msg.get("method") or "")
        req_id = msg.get("id")
        if method == "session/request_permission":
            self._auto_answer_permission(msg)
            return
        if req_id is not None:
            self._queue_protocol_error(
                req_id, -32601, f"method not found: {method}"
            )

    def _auto_answer_permission(self, msg: dict[str, Any]) -> None:
        params = msg.get("params") or {}
        selected = select_kimi_permission_option(params)
        if selected is not None:
            option_id, grant = selected
            result: dict[str, Any] = {
                "outcome": {"outcome": "selected", "optionId": option_id}
            }
            if not grant:
                logger.warning(
                    "no safe kimi grant offered; selecting offered reject "
                    "option %r", option_id,
                )
            self._queue_response(msg.get("id"), result)
        else:
            logger.warning(
                "no safely selectable kimi permission option; replying with "
                "a protocol error"
            )
            self._queue_protocol_error(
                msg.get("id"),
                -32603,
                "no auto-selectable permission option offered",
            )

    def _queue_response(self, req_id: Any, result: dict[str, Any]) -> None:
        if req_id is None or self._proc is None:
            return
        self._spawn_write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _queue_protocol_error(self, req_id: Any, code: int, message: str) -> None:
        if req_id is None or self._proc is None:
            return
        self._spawn_write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
        )

    def _spawn_write(self, frame: dict[str, Any]) -> None:
        """Fire-and-forget an agent-request reply from the reader task."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _write() -> None:
            try:
                await self._write_line(frame, proc)
            except KimiAcpError:
                logger.warning("kimi reply write failed", exc_info=True)

        task = loop.create_task(_write())
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)
