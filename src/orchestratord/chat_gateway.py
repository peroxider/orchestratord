"""Chat gateway — bridges per-run control sockets to SSE/HTTP.

A process-internal component that lives inside the dashboard server.
It connects to per-run Unix domain sockets as another client
(peer to takeover/inject CLI tools), subscribes to real-time event
frames, and exposes them as SSE streams to browser clients.
Inbound HTTP POSTs are translated to control commands written
back to the socket.

Design constraints:
- Pure stdlib (asyncio, threading, queue, json, pathlib).
- Internal asyncio event loop in a dedicated thread manages all
  local control connections; HTTP threads exchange frames via thread-safe
  queues.  This mirrors ``event_tailer.py``'s thread model.
- ToolResult payloads > 4 KB are truncated with ``truncated: true``.
  The full content is available via a separate GET endpoint.
- Supports Unix-domain sockets and loopback TCP endpoints. TCP is used
  on Windows and always binds only to 127.0.0.1.
- Gateway crash must never affect agent runs (all socket ops are
  wrapped in try/except).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import queue
import threading
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MAX_TOOL_RESULT_CHARS = 4096


# ---------------------------------------------------------------------------
# Run connection — one per active agent run
# ---------------------------------------------------------------------------


class _RunConnection:
    """A long-lived UDS connection to one agent run's control socket.

    Reads JSON-line frames from the socket and fans them out to all
    subscriber queues.  Accepts control commands from HTTP threads
    via ``submit()``, which uses ``run_coroutine_threadsafe`` to
    write safely across the thread boundary.
    """

    def __init__(
        self,
        run_id: str,
        endpoint: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.run_id = run_id
        self._endpoint = endpoint
        self._loop = loop
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._subscriber_lock = threading.Lock()
        self._final_frame: dict[str, Any] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: concurrent.futures.Future[None] | None = None

    # -- subscriber management (thread-safe) -------------------------------

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self._subscriber_lock:
            final_frame = self._final_frame
            if final_frame is None:
                self._subscribers.append(q)
        if final_frame is not None:
            q.put_nowait(dict(final_frame))
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._subscriber_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    @property
    def is_finished(self) -> bool:
        """Whether this connection has already published its final state."""
        with self._subscriber_lock:
            return self._final_frame is not None

    @property
    def is_control_ready(self) -> bool:
        """Whether this run currently has a writable control transport."""
        return self._writer is not None and not self.is_finished

    # -- start / stop ------------------------------------------------------

    def start(self) -> None:
        self._reader_task = asyncio.run_coroutine_threadsafe(
            self._read_loop(), self._loop
        )

    def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                self._reader_task.result(timeout=2.0)
            except (concurrent.futures.CancelledError, TimeoutError):
                pass
            self._reader_task = None
        self._finish(
            {"type": "RunEnded", "data": {"run_id": self.run_id}}
        )

    # -- read loop (runs on the asyncio loop thread) -----------------------

    async def _read_loop(self) -> None:
        try:
            if self._endpoint.startswith("tcp://"):
                parsed = urlparse(self._endpoint)
                reader, writer = await asyncio.open_connection(
                    parsed.hostname, parsed.port
                )
            else:
                reader, writer = await asyncio.open_unix_connection(self._endpoint)
            self._writer = writer
        except (OSError, asyncio.TimeoutError) as exc:
            logger.debug(
                "chat_gateway: cannot connect to %s: %s",
                self._endpoint,
                exc,
            )
            self._finish(
                {
                    "type": "RunUnavailable",
                    "data": {"run_id": self.run_id, "reason": "connect_failed"},
                }
            )
            return

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                frame = _truncate_tool_result(frame)
                self._broadcast_nowait(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception:
            logger.exception(
                "chat_gateway: read loop error run_id=%s", self.run_id
            )
        finally:
            self._writer = None
            try:
                writer.close()
            except Exception:
                pass
            self._finish(
                {"type": "RunEnded", "data": {"run_id": self.run_id}}
            )

    # -- broadcast ---------------------------------------------------------

    def _broadcast_nowait(self, frame: dict[str, Any]) -> None:
        with self._subscriber_lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass

    def _finish(self, frame: dict[str, Any]) -> None:
        """Publish one replayable lifecycle frame to current and late readers."""
        with self._subscriber_lock:
            if self._final_frame is not None:
                return
            self._final_frame = frame
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass

    # -- submit control command (thread-safe, called from HTTP threads) ----

    def submit(self, verb: str, payload: str = "") -> bool:
        if self._writer is None:
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._write_line(verb, payload), self._loop
            )
            fut.result(timeout=2.0)
            return True
        except Exception:
            logger.debug(
                "chat_gateway: submit failed run_id=%s verb=%s",
                self.run_id,
                verb,
            )
            return False

    async def _write_line(self, verb: str, payload: str) -> None:
        if self._writer is None:
            return
        line = (
            json.dumps({"cmd": verb, "payload": payload}, ensure_ascii=False)
            + "\n"
        )
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()


# ---------------------------------------------------------------------------
# Frame truncation
# ---------------------------------------------------------------------------


def _truncate_tool_result(frame: dict[str, Any]) -> dict[str, Any]:
    """Truncate ToolResult payloads larger than _MAX_TOOL_RESULT_CHARS."""
    if frame.get("type") != "ToolResultEvent":
        return frame
    data = frame.get("data", {})
    if not isinstance(data, dict):
        return frame
    result = data.get("result")
    if not isinstance(result, dict):
        return frame
    output = result.get("output", "")
    if isinstance(output, str) and len(output) > _MAX_TOOL_RESULT_CHARS:
        result["output"] = output[:_MAX_TOOL_RESULT_CHARS] + "..."
        result["truncated"] = True
        frame["data"] = {**data, "result": result}
    return frame


# ---------------------------------------------------------------------------
# ChatGateway — per-run connection manager
# ---------------------------------------------------------------------------


class ChatGateway:
    """Manages per-run UDS connections and exposes chat API methods.

    Lifecycle mirrors ``EventTailerManager``: the dashboard's
    ``refresh_snapshot`` loop calls ``sync_active_run_ids`` to start/stop
    connections, and HTTP handlers call ``subscribe``/``send_message``/
    ``control`` to interact with specific runs.
    """

    def __init__(self) -> None:
        self._connections: dict[str, _RunConnection] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._start_loop()

    # -- internal asyncio loop ---------------------------------------------

    def _start_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name="chat-gateway-loop",
            daemon=True,
        )
        self._loop_thread.start()

    def _run_loop(self) -> None:
        if self._loop is None:
            return
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def stop(self) -> None:
        with self._lock:
            for conn in list(self._connections.values()):
                conn.stop()
            self._connections.clear()
        if self._loop is not None and self._loop.is_running():
            # ``Future.cancel()`` acknowledges cancellation before the loop
            # has unwound the underlying coroutine. Give it one loop turn so
            # closing a dashboard does not leave a pending reader task.
            try:
                asyncio.run_coroutine_threadsafe(
                    asyncio.sleep(0), self._loop
                ).result(timeout=2.0)
            except (concurrent.futures.CancelledError, TimeoutError, RuntimeError):
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
        if self._loop is not None and not self._loop.is_closed():
            # close() 显式关闭 self-pipe/selector；stopped-but-unclosed 的
            # loop 在解释器退出被 GC 时 __del__ 再 close，可能打印
            # "Exception ignored in BaseEventLoop.__del__"（G3 日志扫描 fail-closed）。
            self._loop.close()
        self._loop = None
        self._loop_thread = None

    # -- sync active runs --------------------------------------------------

    def sync_active_run_ids(
        self,
        run_id_to_endpoint: dict[str, str],
    ) -> None:
        with self._lock:
            for run_id in list(self._connections):
                if run_id not in run_id_to_endpoint:
                    self._connections[run_id].stop()
                    del self._connections[run_id]
            for run_id, endpoint in run_id_to_endpoint.items():
                existing = self._connections.get(run_id)
                if existing is not None and existing.is_finished:
                    del self._connections[run_id]
                    existing = None
                if existing is None:
                    conn = _RunConnection(
                        run_id=run_id,
                        endpoint=endpoint,
                        loop=self._loop,  # type: ignore[arg-type]
                    )
                    self._connections[run_id] = conn
                    conn.start()

    # -- subscribe / unsubscribe -------------------------------------------

    def subscribe(self, run_id: str) -> queue.Queue[dict[str, Any]] | None:
        with self._lock:
            conn = self._connections.get(run_id)
            if conn is None:
                return None
            return conn.subscribe()

    def unsubscribe(self, run_id: str, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            conn = self._connections.get(run_id)
            if conn is not None:
                conn.unsubscribe(q)

    def is_control_ready(self, run_id: str) -> bool:
        """Return whether Pause/Resume/Stop can be delivered for *run_id*."""
        with self._lock:
            conn = self._connections.get(run_id)
            return bool(conn is not None and conn.is_control_ready)

    # -- read history ------------------------------------------------------

    @staticmethod
    def read_history(run_id: str) -> list[dict[str, Any]]:
        from orchestratord.event_tailer import read_history_direct

        return read_history_direct(run_id)

    # -- send message (followup) -------------------------------------------

    def send_message(self, run_id: str, text: str) -> bool:
        with self._lock:
            conn = self._connections.get(run_id)
            if conn is None:
                return False
            return conn.submit("followup", text)

    # -- control verbs -----------------------------------------------------

    def control(self, run_id: str, verb: str, payload: str = "") -> bool:
        with self._lock:
            conn = self._connections.get(run_id)
            if conn is None:
                return False
            return conn.submit(verb, payload)
