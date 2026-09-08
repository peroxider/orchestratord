"""ZeroclawSession — real ACP wire translation for the ``zeroclaw`` CLI.

Wire format (ported from the Go reference ``multica/server/pkg/agent/
zeroclaw.go``, which was verified against a real ZeroClaw 0.8.4 binary):
ZeroClaw is driven as ``zeroclaw acp`` — an ACP (Agent Client Protocol)
JSON-RPC 2.0 exchange, one frame per line (NDJSON) over stdin/stdout.
One turn is one process::

    initialize
    → session/new (fresh)  or  session/resume (resume; NEVER session/load,
      which replays the whole retained transcript back as notifications)
    → session/prompt {sessionId, prompt:[{type:"text", text}]}
    → session/update notifications (agent_message_chunk, tool_call, …)
    → session/prompt response {stopReason, usage?}

Inbound notifications (``params.update.sessionUpdate``):
    agent_message_chunk {content:{type:"text",text}} → TEXT_DELTA
    tool_call / tool_call_update                     → TOOL_CALL / TOOL_RESULT

ZeroClaw pushes transcript replays before answering ``session/resume``
(what ``session/load`` does for real), so session updates are gated until
``session/prompt`` is in flight — the Go reference's
``streamingCurrentTurn`` gate — so a resumed turn never re-emits the
prior answer as its own output.

Server→client ``session/request_permission`` requests are auto-answered
on the shared ACP policy (session-scoped grant → allow_once → offered
reject_once; anything else is a -32603 protocol error). ZeroClaw's
legacy structured-question bridge (every optionId ``choice-N``) fails
closed, exactly as ``selectZeroclawPermissionOption`` does in Go.

Departures / limitations vs the Go daemon, documented per §8.2.3:
  - argv filtering beyond lifting ``--agent``/``--agent-alias`` out into
    the ``agentAlias`` param (acp, --help, login, auth blocking) stays
    daemon-side; the remaining custom args are passed through as-is.
  - stderr provider-error sniffing / completed→failed promotion is not
    replicated; failures surface as ERROR envelopes.
  - per-request timeouts rely on the run-level ``total_timeout_s``
    watchdog instead of a per-RPC context deadline.
  - ``spec.model`` is deliberately NOT put on argv: ``zeroclaw acp`` has
    no model flag and no ``session/set_model`` method — the model belongs
    to the ZeroClaw agent profile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

_DEFAULT_TOTAL_TIMEOUT_S = 600.0

_logger = logging.getLogger(__name__)

# Grace windows mirroring zeroclawReaderDrainGrace / zeroclawNotificationQuietTime.
_READER_DRAIN_GRACE_S = 2.0
_NOTIFICATION_QUIET_S = 0.25
_PROCESS_EXIT_GRACE_S = 0.5

# ACP PermissionOptionKind classification (hermes.go acpKind*). Only the
# two allow kinds grant; unknown kinds fail closed.
_GRANT_KINDS = frozenset({"allow_once", "allow_always"})
_SESSION_SCOPED_OPTION_IDS = ("allow_session", "approve_for_session")

# Codes under which runtimes report unknown sessions; the wording check
# still decides (a -32000 rate limit must not read as a lost session).
_SESSION_NOT_FOUND_CODES = frozenset({-32603, -32602, -32002, -32000})

_USAGE_KEY_MAP = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadTokens": "cache_read_tokens",
    "cacheWriteTokens": "cache_write_tokens",
    "costUsdTicks": "cost_usd_ticks",
}

_UPDATE_TYPES = {
    "agentmessagechunk": "agent_message_chunk",
    "agentthoughtchunk": "agent_thought_chunk",
    "toolcall": "tool_call",
    "toolcallupdate": "tool_call_update",
    "usageupdate": "usage_update",
    "turnend": "turn_end",
    "endturn": "turn_end",
}


def _normalize_update_type(update: dict[str, Any]) -> str:
    """Port of ``normalizeACPUpdate``: accept the ``sessionUpdate`` key,
    the ``type`` key, or an externally-tagged single-key wrapper."""
    kind = update.get("sessionUpdate") or update.get("type")
    if isinstance(kind, str) and kind:
        key = kind.strip().replace("_", "").replace("-", "").lower()
        return _UPDATE_TYPES.get(key, "")
    if len(update) == 1:
        (key,) = update.keys()
        key = key.strip().replace("_", "").replace("-", "").lower()
        return _UPDATE_TYPES.get(key, "")
    return ""


def _take_agent_alias(args: list[str]) -> tuple[str, list[str]]:
    """Port of ``takeZeroclawAgentAlias``.

    ``zeroclaw acp`` has no ``--agent`` flag — clap aborts on one — so the
    alias is lifted out of custom args and travels as the session/new
    ``agentAlias`` param instead. Both ``--agent x`` and ``--agent=x`` are
    accepted, ``--agent-alias`` is the long spelling, last occurrence
    wins, and an empty/whitespace value counts as unset.
    """
    alias = ""
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        flag, value = arg, ""
        has_inline = False
        idx = arg.find("=")
        if idx > 0:
            flag, value, has_inline = arg[:idx], arg[idx + 1:], True
        if flag not in ("--agent", "--agent-alias"):
            rest.append(arg)
            i += 1
            continue
        if not has_inline:
            if i + 1 >= len(args):
                i += 1
                continue
            i += 1
            value = args[i]
        alias = value.strip()
        i += 1
    return alias, rest


def _is_session_not_found(error: _ZeroclawRPCError) -> bool:
    """Port of ``isACPSessionNotFound`` (ZeroClaw branch): custom -32000
    SESSION_NOT_FOUND, decided by wording since -32000 is also a generic
    server-error code a transient failure may ride on."""
    if error.code not in _SESSION_NOT_FOUND_CODES:
        return False
    text = f"{error.message} {error.data}".lower()
    return (
        "session not found" in text
        or "no session found" in text
        or "unknown session" in text
    )


def _select_permission_option(params: dict[str, Any]) -> tuple[str, bool]:
    """Port of ``selectZeroclawPermissionOption`` + the shared ACP policy.

    Returns ``(optionId, ok)``. ``ok=False`` means nothing safely
    selectable was offered and the caller must answer with a -32603
    protocol error rather than fabricate an outcome.
    """
    options = params.get("options")
    if not isinstance(options, list):
        return "", False
    parsed: list[tuple[str, str]] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        option_id = str(opt.get("optionId", ""))
        kind = str(opt.get("kind", "")).strip().lower()
        parsed.append((option_id, kind))

    # ZeroClaw's legacy structured-question bridge labels every answer
    # choice-N: fail closed so generic auto-approval cannot silently
    # answer every question with the first choice.
    if len(parsed) >= 2 and parsed and all(
        option_id.startswith("choice-") for option_id, _ in parsed
    ):
        return "", False

    # 1. Known session-scoped grant ids, when offered with a grant kind.
    for want in _SESSION_SCOPED_OPTION_IDS:
        for option_id, kind in parsed:
            if option_id == want and kind in _GRANT_KINDS:
                return option_id, True
    # 2. Any single-use grant (inherently scoped to this one action).
    for option_id, kind in parsed:
        if option_id and kind == "allow_once":
            return option_id, True
    # 3. No safe grant: deny THIS action via an offered reject_once.
    for option_id, kind in parsed:
        if option_id and kind == "reject_once":
            return option_id, False
    # 4. Empty / malformed / permanent-only / reject_always-only.
    return "", False


class _ZeroclawRPCError(Exception):
    """JSON-RPC error frame returned by the ZeroClaw agent process."""

    def __init__(self, method: str, code: int, message: str, data: str = "") -> None:
        super().__init__(message)
        self.method = method
        self.code = code
        self.message = message
        self.data = data

    def __str__(self) -> str:
        base = f"{self.method}: {self.message} (code={self.code})"
        if self.data:
            base += f", data={self.data}"
        return base


class _ZeroclawTurnError(Exception):
    """Protocol-level turn failure; translated into an ERROR envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ZeroclawSession:
    """Adapt the ``zeroclaw acp`` ACP/JSON-RPC stream into the SPI.

    Spawn-per-turn Cli model: each ``send`` spawns ``zeroclaw acp``,
    performs the full ACP handshake, drives one ``session/prompt`` turn,
    and tears the process down. The CLI genuinely streams
    ``agent_message_chunk`` deltas during the turn, so ``streaming_deltas``
    is now claimed (descriptor + backend capabilities kept in lockstep).
    """

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"zeroclaw-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            resume_detection=False,
        )
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        # Per-turn state (one process per send()).
        self._request_seq = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_tools: dict[str, dict[str, Any]] = {}
        self._in_turn = False
        self._native_session_id: str | None = None
        self._turn_usage: dict[str, Any] | None = None

    # -- envelope plumbing -------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
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

    async def _emit_events(self) -> AsyncIterator[EventEnvelope]:
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    # -- turn driver ---------------------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        text = content if isinstance(content, str) else str(content)
        reason = "success"
        try:
            timeout = self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S
            reason = await asyncio.wait_for(self._run_turn(text), timeout=timeout)
        except TimeoutError:
            reason = "timeout"
            self._emit(
                EventKind.ERROR,
                {
                    "code": "zeroclaw_timeout",
                    "message": (
                        f"zeroclaw exceeded {timeout:.1f}s (total_timeout_s)"
                    ),
                },
            )
        except FileNotFoundError as exc:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": "zeroclaw_spawn_error", "message": str(exc)},
            )
        except _ZeroclawTurnError as exc:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": exc.code, "message": exc.message},
            )
        except _ZeroclawRPCError as exc:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": "zeroclaw_rpc", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": "zeroclaw_error", "message": f"{type(exc).__name__}: {exc}"},
            )

        complete: dict[str, Any] = {"reason": reason}
        if self._native_session_id:
            complete["session_id"] = self._native_session_id
        if self._turn_usage:
            complete["usage"] = self._turn_usage
        self._emit(EventKind.TURN_COMPLETE, {"reason": reason})
        self._emit(EventKind.SESSION_COMPLETE, complete)

    async def _run_turn(self, text: str) -> str:
        argv, agent_alias = self._build_argv()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._spec.cwd or None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self._spec.env},
        )
        self._request_seq = 0
        self._pending = {}
        self._pending_tools = {}
        self._native_session_id = None
        self._turn_usage = None
        reader = asyncio.create_task(self._reader_loop(proc))
        try:
            return await self._handshake_and_prompt(proc, text, agent_alias)
        finally:
            await self._teardown(proc, reader)

    def _build_argv(self) -> tuple[list[str], str]:
        command = self._spec.extra.get("command")
        if isinstance(command, list) and command:
            executable = [str(part) for part in command]
        else:
            executable = [self._spec.runtime_bin or "zeroclaw"]
        custom_args = self._spec.extra.get("custom_args")
        args = [str(a) for a in custom_args] if isinstance(custom_args, list) else []
        agent_alias, rest = _take_agent_alias(args)
        return [*executable, "acp", *rest], agent_alias

    async def _teardown(
        self,
        proc: asyncio.subprocess.Process,
        reader: asyncio.Task[None],
    ) -> None:
        self._in_turn = False
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                _logger.debug("zeroclaw stdin close failed", exc_info=True)
        try:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), _PROCESS_EXIT_GRACE_S)
                except TimeoutError:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), _PROCESS_EXIT_GRACE_S)
        except (TimeoutError, ProcessLookupError):
            if proc.returncode is None:
                proc.kill()
            try:
                await proc.wait()
            except Exception:
                _logger.debug(
                    "zeroclaw reap after kill failed", exc_info=True
                )
        except Exception:  # noqa: BLE001 - best-effort teardown
            if proc.returncode is None:
                proc.kill()
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        except Exception:
            _logger.debug("zeroclaw reader teardown failed", exc_info=True)
        self._fail_pending(
            _ZeroclawTurnError("zeroclaw_exit", "zeroclaw process torn down")
        )

    # -- JSON-RPC transport --------------------------------------------------

    async def _reader_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            await self._dispatch_frame(msg, proc)
        self._fail_pending(
            _ZeroclawTurnError(
                "zeroclaw_exit",
                "zeroclaw process exited before responding (stdout closed)",
            )
        )

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _dispatch_frame(
        self,
        msg: dict[str, Any],
        proc: asyncio.subprocess.Process,
    ) -> None:
        method = msg.get("method")
        if method is not None:
            if msg.get("id") is not None:
                await self._handle_agent_request(msg, proc)
            else:
                self._handle_notification(msg)
            return
        future = self._pending.pop(msg.get("id"), None)
        if future is None or future.done():
            return
        if "result" in msg or "error" in msg:
            future.set_result(msg)
        else:
            future.set_exception(
                _ZeroclawTurnError(
                    "zeroclaw_protocol", f"malformed JSON-RPC frame: {msg!r:.200}"
                )
            )

    async def _request(
        self,
        proc: asyncio.subprocess.Process,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        if proc.returncode is not None:
            raise _ZeroclawTurnError(
                "zeroclaw_exit",
                f"zeroclaw process exited (returncode={proc.returncode})",
            )
        assert proc.stdin is not None
        self._request_seq += 1
        req_id = self._request_seq
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        msg = await future
        err = msg.get("error")
        if isinstance(err, dict):
            raise _ZeroclawRPCError(
                method,
                int(err.get("code", 0) or 0),
                str(err.get("message", "")),
                str(err.get("data", "") or ""),
            )
        return msg.get("result")

    async def _handle_agent_request(
        self,
        msg: dict[str, Any],
        proc: asyncio.subprocess.Process,
    ) -> None:
        assert proc.stdin is not None
        method = str(msg.get("method"))
        if method == "session/request_permission":
            option_id, ok = _select_permission_option(msg.get("params") or {})
            if ok:
                # Select an offered option — a safe grant, or an offered
                # reject_once to deny just this action (never a whole-turn
                # "cancelled", which aborts the prompt on other ACP agents).
                response = {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "result": {
                        "outcome": {"outcome": "selected", "optionId": option_id}
                    },
                }
            else:
                # Nothing safely selectable (or the fail-closed legacy
                # choice bridge): a protocol error, not a fabricated
                # outcome. ZeroClaw then fails the prompt itself.
                response = {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {
                        "code": -32603,
                        "message": "no auto-selectable permission option offered",
                    },
                }
        else:
            # Unknown agent→client request — reply "method not found" so
            # the agent doesn't block waiting for us.
            response = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {
                    "code": -32601,
                    "message": f"method not found: {method}",
                },
            }
        try:
            proc.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except Exception:
            _logger.debug(
                "zeroclaw permission/response write failed", exc_info=True
            )

    # -- inbound translation ---------------------------------------------------

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        if msg.get("method") not in ("session/update", "session/notification"):
            return
        params = msg.get("params") or {}
        update = params.get("update")
        if not isinstance(update, dict):
            return
        utype = _normalize_update_type(update)
        if not self._in_turn:
            # Turn gate: anything pushed outside our prompt — session/load
            # style transcript replays around session/resume — is dropped.
            return
        if utype == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if text is None:
                text = content if isinstance(content, str) else ""
            if text:
                self._emit(
                    EventKind.TEXT_DELTA, {"text": str(text), "delta": str(text)}
                )
        elif utype == "tool_call":
            self._handle_tool_call_start(update)
        elif utype == "tool_call_update":
            self._handle_tool_call_update(update)
        elif utype == "agent_thought_chunk":
            # No SPI kind for thinking; the Go daemon maps it to a thinking
            # message the Python runner likewise has no sink for.
            return
        else:
            self._emit(
                EventKind.UNKNOWN, {"event": utype or "unknown", "raw": dict(update)}
            )

    def _tool_input(self, update: dict[str, Any]) -> dict[str, Any] | None:
        for key in ("rawInput", "input", "parameters"):
            value = update.get(key)
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _content_blocks_text(blocks: Any) -> str:
        if not isinstance(blocks, list):
            return ""
        pieces: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "content":
                inner = block.get("content")
                if isinstance(inner, dict) and inner.get("type") == "text":
                    text = inner.get("text")
                    if isinstance(text, str) and text:
                        pieces.append(text)
            elif block.get("type") == "diff":
                path = str(block.get("path", ""))
                new_text = block.get("newText")
                pieces.append(
                    f"--- {path}\n+++ {path}\n{new_text if isinstance(new_text, str) else ''}"
                )
        return "\n".join(pieces)

    @staticmethod
    def _raw_output_text(raw: Any) -> str:
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        try:
            return json.dumps(raw)
        except (TypeError, ValueError):
            return str(raw)

    @staticmethod
    def _parse_tool_args(args_text: str) -> dict[str, Any]:
        args_text = args_text.strip()
        if not args_text:
            return {}
        try:
            parsed = json.loads(args_text)
        except json.JSONDecodeError:
            return {"text": args_text}
        return parsed if isinstance(parsed, dict) else {"text": args_text}

    def _handle_tool_call_start(self, update: dict[str, Any]) -> None:
        call_id = str(update.get("toolCallId", ""))
        name = str(update.get("name") or update.get("title") or "")
        tool_input = self._tool_input(update)
        if tool_input is not None:
            # The start frame carries the invocation: emit immediately so
            # the UI sees the tool call live (Go hermes rawInput path).
            self._pending_tools[call_id] = {"emitted": True}
            self._emit(
                EventKind.TOOL_CALL,
                {"call_id": call_id, "name": name, "arguments": tool_input},
            )
            return
        # No input yet (args may stream across updates): buffer and defer
        # the TOOL_CALL to completion so the UI never sees a partial
        # ``{""`` argument blob (Go kimi-style deferred emission).
        self._pending_tools[call_id] = {
            "name": name,
            "args_text": self._content_blocks_text(update.get("content")),
            "emitted": False,
        }

    def _handle_tool_call_update(self, update: dict[str, Any]) -> None:
        call_id = str(update.get("toolCallId", ""))
        status = str(update.get("status", ""))
        if status not in ("completed", "failed"):
            # Mid-stream: overwrite (not append) the cumulative args text.
            pending = self._pending_tools.get(call_id)
            if pending is not None and not pending["emitted"]:
                text = self._content_blocks_text(update.get("content"))
                if text:
                    pending["args_text"] = text
                    if update.get("name") or update.get("title"):
                        pending["name"] = str(
                            update.get("name") or update.get("title")
                        )
            return

        pending = self._pending_tools.pop(call_id, None)
        if pending is not None and not pending["emitted"]:
            tool_input = self._tool_input(update)
            if tool_input is None:
                tool_input = self._parse_tool_args(pending.get("args_text", ""))
            self._emit(
                EventKind.TOOL_CALL,
                {
                    "call_id": call_id,
                    "name": str(pending.get("name", "")),
                    "arguments": tool_input,
                },
            )
        elif pending is None:
            # Completion arrived without a start frame: synthesize minimal
            # info so the UI at least sees the tool name (Go fallback).
            tool_input = self._tool_input(update)
            name = str(update.get("name") or update.get("title") or "")
            if tool_input is not None or name:
                self._emit(
                    EventKind.TOOL_CALL,
                    {
                        "call_id": call_id,
                        "name": name,
                        "arguments": tool_input or {},
                    },
                )

        output = self._raw_output_text(update.get("rawOutput"))
        if not output:
            output = self._raw_output_text(update.get("output"))
        if not output:
            output = self._content_blocks_text(update.get("content"))
        self._emit(
            EventKind.TOOL_RESULT,
            {
                "call_id": call_id,
                "ok": status == "completed",
                "status": status,
                "output": output,
            },
        )

    # -- handshake and prompt ----------------------------------------------------

    async def _handshake_and_prompt(
        self,
        proc: asyncio.subprocess.Process,
        text: str,
        agent_alias: str,
    ) -> str:
        init_result = await self._request(
            proc,
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "orchestratord", "version": "0.1.0"},
                "clientCapabilities": {},
            },
        ) or {}

        if self._spec.resume_session_id:
            if not self._resume_supported(init_result):
                raise _ZeroclawTurnError(
                    "zeroclaw_resume_unavailable",
                    "zeroclaw session/resume unavailable: initialize did not "
                    "advertise sessionCapabilities.resume (persistence is "
                    "unavailable); retry from a rebuilt fresh-session context",
                )
            # session/resume, NOT session/load: load replays every retained
            # message back as session/update notifications, so a resumed
            # turn would re-emit the previous answer as its own output.
            try:
                result = await self._request(
                    proc,
                    "session/resume",
                    {"sessionId": self._spec.resume_session_id},
                ) or {}
            except _ZeroclawRPCError as exc:
                if _is_session_not_found(exc):
                    raise _ZeroclawTurnError(
                        "zeroclaw_resume_rejected",
                        f"zeroclaw session/resume failed: {exc}",
                    ) from exc
                raise _ZeroclawTurnError(
                    "zeroclaw_resume", f"zeroclaw session/resume failed: {exc}"
                ) from exc
            # resume answers a bare {}; fall back to the requested id.
            self._native_session_id = (
                result.get("sessionId") or self._spec.resume_session_id
            )
        else:
            params: dict[str, Any] = {"cwd": self._spec.cwd or ".", "mcpServers": []}
            if agent_alias:
                params["agentAlias"] = agent_alias
            try:
                result = await self._request(proc, "session/new", params) or {}
            except _ZeroclawRPCError as exc:
                message = f"zeroclaw session/new failed: {exc}"
                if "agentAlias" in str(exc.message):
                    message += (
                        " — ZeroClaw auto-selects an agent only when its config"
                        " holds exactly one [agents.<alias>] entry. Configure"
                        " the agent you want, then name it with `--agent <alias>`"
                        " in this runtime's custom args or set"
                        " `[acp].default_agent` in ZeroClaw's config."
                    )
                raise _ZeroclawTurnError("zeroclaw_session_new", message) from exc
            session_id = result.get("sessionId")
            if not session_id:
                raise _ZeroclawTurnError(
                    "zeroclaw_session_new", "zeroclaw session/new returned no session ID"
                )
            self._native_session_id = str(session_id)

        self.session_id = self._native_session_id or self.session_id

        user_text = text
        if self._spec.system_prompt:
            user_text = f"{self._spec.system_prompt}\n\n---\n\n{text}"

        # Flip the turn gate just before session/prompt so transcript
        # replays pushed during the handshake are swallowed.
        self._in_turn = True
        try:
            prompt_result = await self._request(
                proc,
                "session/prompt",
                {
                    "sessionId": self._native_session_id,
                    "prompt": [{"type": "text", "text": user_text}],
                },
            ) or {}
        except _ZeroclawRPCError as exc:
            raise _ZeroclawTurnError(
                "zeroclaw_prompt", f"zeroclaw session/prompt failed: {exc}"
            ) from exc

        # Give the stdout reader a bounded chance to consume notifications
        # ZeroClaw may emit just after session/prompt returns (late
        # agent_message_chunk frames) — port of waitForACPNotificationQuiescence.
        await self._drain_quiescence()

        usage = prompt_result.get("usage")
        if isinstance(usage, dict) and usage:
            self._turn_usage = {
                _USAGE_KEY_MAP.get(key, key): value for key, value in usage.items()
            }
        if prompt_result.get("stopReason") == "cancelled":
            return "cancelled"
        return "success"

    @staticmethod
    def _resume_supported(init_result: dict[str, Any]) -> bool:
        agent_caps = init_result.get("agentCapabilities")
        if not isinstance(agent_caps, dict):
            return False
        session_caps = agent_caps.get("sessionCapabilities")
        return isinstance(session_caps, dict) and "resume" in session_caps

    async def _drain_quiescence(self) -> None:
        deadline = time.monotonic() + _READER_DRAIN_GRACE_S
        last_count = len(self._events)
        while time.monotonic() < deadline:
            await asyncio.sleep(_NOTIFICATION_QUIET_S)
            if len(self._events) == last_count:
                return
            last_count = len(self._events)

    # -- SPI surface without wire support -----------------------------------------

    async def interrupt(self) -> None:
        return None

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        return None

    async def probe_resume(self) -> ResumeStatus:
        """zeroclaw has no cross-process resume probe (resume_detection=False)."""
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
