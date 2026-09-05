"""Isolated ClawCodex execution behind the unchanged AgentSession interface."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, fields
from typing import Any

import psutil

from orchestratord.process_control import ProcessTree
from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus


class ClawcodexProcessSession:
    """One owned worker per conversation, with acknowledged control messages."""

    def __init__(self, spec: SessionSpec, capabilities: BackendCapabilities) -> None:
        self._spec = spec
        self.capabilities = capabilities
        self.session_id = spec.resume_session_id or ""
        self._process: asyncio.subprocess.Process | None = None
        self._tree: ProcessTree | None = None
        self._launch_task: asyncio.Task | None = None
        self._init_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._send_task: asyncio.Task | None = None
        self._turn_done: asyncio.Future | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._queue: asyncio.Queue[EventEnvelope | None] = asyncio.Queue()
        self._control_lock = asyncio.Lock()
        self._request_id = 0
        self._seq = 0
        self._active = False
        self._terminal_seen = False
        self._closed = False
        self._paused_at: float | None = None
        self._paused_seconds = 0.0
        self._stderr_tail = ""

    def _active_clock(self) -> float:
        now = time.monotonic()
        paused = now - self._paused_at if self._paused_at is not None else 0.0
        return now - self._paused_seconds - paused

    async def _launch(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "orchestratord_clawcodex.worker",
            cwd=self._spec.cwd,
            env={**os.environ, **self._spec.env},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
            limit=16 * 1024 * 1024,
        )
        self._tree = ProcessTree(self._process.pid)
        self._reader_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        if self._closed:
            await asyncio.to_thread(self._tree.kill)

    async def _initialize(self) -> None:
        await asyncio.shield(self._launch_task)
        if self._closed:
            raise RuntimeError("session closed")
        values = {field.name: getattr(self._spec, field.name) for field in fields(self._spec)}
        if self._spec.approval is not None:
            values["approval"] = asdict(self._spec.approval)
        try:
            json.dumps(values)
        except (TypeError, ValueError) as exc:
            raise ValueError("ClawCodex worker requires serializable SessionSpec values; in-process callbacks are not supported") from exc
        await self._request("init", spec=values)

    async def _ensure_worker(self) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        if self._launch_task is None:
            self._launch_task = asyncio.create_task(self._launch())
            self._init_task = asyncio.create_task(self._initialize())
        await asyncio.shield(self._init_task)

    async def _request(self, command: str, **payload: Any) -> Any:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeError("ClawCodex worker is not running")
        self._request_id += 1
        request_id = self._request_id
        reply = asyncio.get_running_loop().create_future()
        self._pending[request_id] = reply
        try:
            process.stdin.write((json.dumps({"id": request_id, "command": command, **payload}) + "\n").encode())
            await process.stdin.drain()
            started = self._active_clock()
            timeout = self._spec.handshake_timeout_s or 30.0
            while not reply.done():
                if self._active_clock() - started >= timeout:
                    raise TimeoutError(f"ClawCodex worker did not acknowledge {command}")
                await asyncio.wait({reply}, timeout=.05)
            return reply.result()
        finally:
            self._pending.pop(request_id, None)
            if not reply.done():
                reply.cancel()

    def _emit(self, kind: EventKind, payload: dict[str, Any]) -> None:
        self._seq += 1
        self._terminal_seen |= kind is EventKind.SESSION_COMPLETE
        self._queue.put_nowait(EventEnvelope(self._seq, time.time(), kind, payload))

    def _finish_turn(self) -> None:
        if self._active:
            self._active = False
            self._queue.put_nowait(None)
            if self._turn_done is not None and not self._turn_done.done():
                self._turn_done.set_result(None)

    async def _read_messages(self) -> None:
        process = self._process
        failure = "ClawCodex worker exited before completing the request"
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message.get("type") == "event":
                    self._seq += 1
                    event = EventEnvelope(
                        seq=self._seq, timestamp=message["timestamp"],
                        kind=EventKind(message["kind"]), payload=message["payload"],
                    )
                    if native_id := event.payload.get("session_id"):
                        self.session_id = native_id
                    self._terminal_seen |= event.kind is EventKind.SESSION_COMPLETE
                    self._queue.put_nowait(event)
                    self._tree.remember_descendants()
                elif message.get("type") == "turn_end":
                    if self._active and not self._terminal_seen:
                        self._emit(EventKind.SESSION_COMPLETE, {"reason": "worker_error"})
                    self._finish_turn()
                elif message.get("type") == "reply":
                    reply = self._pending.get(message.get("id"))
                    if reply is not None and not reply.done():
                        if message.get("error"):
                            reply.set_exception(RuntimeError(message["error"]))
                        else:
                            reply.set_result(message.get("result"))
        except (ValueError, KeyError, OSError, psutil.Error) as exc:
            failure = f"Invalid ClawCodex worker stream: {type(exc).__name__}"
            await asyncio.to_thread(self._tree.kill)
        finally:
            await process.wait()
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_exception(RuntimeError("session closed" if self._closed else failure))
            if self._active and not self._terminal_seen:
                if not self._closed:
                    self._emit(EventKind.ERROR, {"message": failure})
                self._emit(EventKind.SESSION_COMPLETE, {"reason": "stopped" if self._closed else "worker_error"})
            self._finish_turn()

    async def _read_stderr(self) -> None:
        while chunk := await self._process.stderr.read(4096):
            # Keep bounded diagnostics private; never mix logs with text events.
            self._stderr_tail = (self._stderr_tail + chunk.decode(errors="replace"))[-16384:]

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        if self._active:
            await asyncio.shield(self._turn_done)
        if self._closed:
            raise RuntimeError("session closed")
        self._active = True
        self._terminal_seen = False
        self._turn_done = asyncio.get_running_loop().create_future()
        self._send_task = asyncio.create_task(self._submit(content if isinstance(content, str) else str(content)))

    async def _submit(self, content: str | list[Any]) -> None:
        try:
            await self._ensure_worker()
            await self._request("send", content=content)
        except (RuntimeError, ValueError, OSError, TimeoutError) as exc:
            if not self._closed and self._active:
                self._emit(EventKind.ERROR, {"message": str(exc)})
                self._emit(EventKind.SESSION_COMPLETE, {"reason": "worker_error"})
                self._finish_turn()

    async def events(self) -> AsyncIterator[EventEnvelope]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        await self._ensure_worker()
        await self._request("approve", request_id=request_id, decision=decision.value)

    async def probe_resume(self) -> ResumeStatus:
        if not (self._spec.resume_session_id or "").strip():
            return ResumeStatus.RESUMED
        await self._ensure_worker()
        return ResumeStatus(await self._request("probe_resume"))

    async def interrupt(self) -> None:
        raise RuntimeError("ClawCodex interrupt is not supported; use close to stop")

    async def pause(self) -> None:
        async with self._control_lock:
            if self._closed or not self._active:
                raise RuntimeError("No active ClawCodex execution to pause")
            if self._paused_at is not None:
                return
            # send() is non-blocking; let its startup task publish the process.
            while self._launch_task is None and self._active and not self._closed:
                await asyncio.sleep(0)
            if self._launch_task is None:
                raise RuntimeError("No ClawCodex process to pause")
            await asyncio.shield(self._launch_task)
            await asyncio.to_thread(self._tree.pause)
            self._paused_at = time.monotonic()

    async def resume(self) -> None:
        async with self._control_lock:
            if self._closed:
                raise RuntimeError("session closed")
            if self._paused_at is None:
                return
            await asyncio.to_thread(self._tree.resume)
            self._paused_seconds += time.monotonic() - self._paused_at
            self._paused_at = None

    async def close(self) -> None:
        async with self._control_lock:
            self._closed = True
            if self._launch_task is None:
                if self._send_task is not None:
                    await self._send_task
                if self._active:
                    self._emit(EventKind.SESSION_COMPLETE, {"reason": "stopped"})
                    self._finish_turn()
                return
            try:
                await asyncio.shield(self._launch_task)
            except OSError:
                if self._send_task is not None:
                    await self._send_task
                if self._active:
                    self._emit(EventKind.SESSION_COMPLETE, {"reason": "stopped"})
                    self._finish_turn()
                return
            process = self._process
            if process.returncode is None:
                self._tree.remember_descendants()
                if self._active or self._paused_at is not None:
                    await asyncio.to_thread(self._tree.kill)
                else:
                    try:
                        await asyncio.wait_for(self._request("close"), 2)
                        await asyncio.wait_for(process.wait(), 2)
                    except (RuntimeError, TimeoutError, BrokenPipeError, ConnectionResetError):
                        await asyncio.to_thread(self._tree.kill)
            await asyncio.wait_for(process.wait(), 3)
            # Also remove tools orphaned during normal runtime shutdown.
            await asyncio.to_thread(self._tree.kill)
            await asyncio.gather(*[task for task in (
                self._reader_task, self._stderr_task, self._send_task, self._init_task,
            ) if task is not None], return_exceptions=True)

    def close_sync(self) -> None:
        self._closed = True
        if self._tree is not None:
            self._tree.kill()
        elif self._active:
            self._emit(EventKind.SESSION_COMPLETE, {"reason": "stopped"})
            self._finish_turn()
