"""WorkerManager — JSON-RPC 2.0 over stdio for agent worker processes.

Reconstructed during source recovery: the original module was lost with
the deleted source tree; this implementation was rebuilt from the
call-site contract in ``backends/orchestratord-codex`` (the only
consumer) and the transcript outline (``class WorkerManager`` /
``async def start`` / ``async def stop``).

Framing: newline-delimited JSON-RPC 2.0. Each ``request`` line carries
an integer ``id``; responses are matched back via pending futures.
Notifications (no ``id``) are kept in a bounded deque for diagnostics.

Public surface (exactly what the codex app-server backend needs):

* ``WorkerManager(worker_cmd=..., cwd=..., env=...)``
* ``await start()``                       — spawn the worker + reader loop
* ``await session_prompt(sid, text)``     — JSON-RPC ``session/prompt``
* ``await session_cancel(sid)``           — JSON-RPC ``session/cancel``
* ``await session_load(sid)``             — JSON-RPC ``session/load``
* ``await stop()``                        — terminate worker, fail pending
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hard cap on retained notifications — they are diagnostics only.
_MAX_NOTIFICATIONS = 256

# Default per-request timeout. Prompts legitimately run for minutes
# (a full agent turn), so the default is generous; callers can override
# per call via ``timeout_s``.
_DEFAULT_REQUEST_TIMEOUT_S = 3600.0


class WorkerProcessError(RuntimeError):
    """The worker process died or never started."""


def _record_worker_crash(detail: str) -> None:
    """Best-effort crash telemetry for backend worker process death."""
    try:
        from ..telemetry import record_crash

        record_crash(kind="backend_worker", detail=detail[:200])
    except Exception:
        pass


class WorkerManager:
    """Owns one agent worker subprocess speaking newline-delimited JSON-RPC.

    The manager is transport-only: it knows nothing about the codex
    app-server schema beyond request method names, and simply resolves
    each request with the raw JSON-RPC response object
    (``{"result": ...}`` or ``{"error": {"code", "message"}}``).
    """

    def __init__(
        self,
        worker_cmd: list[str],
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._worker_cmd = list(worker_cmd)
        self._cwd = str(cwd) if cwd is not None else None
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._notifications: deque[dict[str, Any]] = deque(
            maxlen=_MAX_NOTIFICATIONS
        )
        self._stopped = False

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Spawn the worker process and start the response reader loop."""
        if self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._worker_cmd,
                cwd=self._cwd,
                env=self._env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            _record_worker_crash(f"failed to spawn worker {self._worker_cmd!r}: {exc}")
            raise WorkerProcessError(
                f"failed to spawn worker {self._worker_cmd!r}: {exc}"
            ) from exc
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="bridge-worker-reader"
        )
        logger.info("worker started: %s (pid=%s)", self._worker_cmd, self._process.pid)

    async def stop(self) -> None:
        """Terminate the worker, fail pending requests, reclaim resources."""
        if self._stopped:
            return
        self._stopped = True
        proc, self._process = self._process, None
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
                proc.kill()
                await proc.wait()
        self._fail_pending(WorkerProcessError("worker stopped"))

    # -- RPC surface ------------------------------------------------------

    async def session_prompt(self, session_id: str, text: str) -> dict[str, Any]:
        """Run one agent turn; resolves with the raw JSON-RPC response."""
        return await self._request(
            "session/prompt",
            {"session_id": session_id, "text": text},
        )

    async def session_cancel(self, session_id: str) -> dict[str, Any]:
        """Interrupt the in-flight turn for ``session_id``."""
        return await self._request(
            "session/cancel",
            {"session_id": session_id},
            timeout_s=30.0,
        )

    async def session_load(self, session_id: str) -> dict[str, Any]:
        """Probe a persisted session; ``result.exists`` reports reachability."""
        return await self._request(
            "session/load",
            {"session_id": session_id},
            timeout_s=30.0,
        )

    # -- internals --------------------------------------------------------

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        proc = self._process
        if proc is None or proc.stdin is None:
            raise WorkerProcessError("worker not started")
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            line = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": method,
                    "params": params,
                }
            )
            try:
                proc.stdin.write(line.encode("utf-8") + b"\n")
                await proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:
                self._pending.pop(req_id, None)
                raise WorkerProcessError(f"worker stdin write failed: {exc}") from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise

    async def _read_loop(self) -> None:
        proc = self._process
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break  # EOF — worker exited
                try:
                    msg = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.warning("worker sent non-JSON line: %.200r", raw)
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("worker reader loop crashed")
        finally:
            # A worker exiting while the manager is still running is a
            # crash; the same finally also fires after a graceful stop()
            # (reader cancelled) — guarded by _stopped.
            if not self._stopped:
                _record_worker_crash(
                    f"worker exited (returncode={proc.returncode})"
                )
            self._fail_pending(
                WorkerProcessError(
                    f"worker exited (returncode={proc.returncode})"
                )
            )

    def _dispatch(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        if req_id is None:
            # JSON-RPC notification — diagnostics only.
            self._notifications.append(msg)
            return
        future = self._pending.pop(req_id, None)
        if future is None or future.done():
            logger.debug("response for unknown request id %r", req_id)
            return
        if msg.get("error") is not None or "result" in msg:
            future.set_result(msg)
        else:
            future.set_exception(
                WorkerProcessError(f"malformed JSON-RPC response: {msg!r:.200}")
            )

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)


if sys.version_info >= (3, 9):  # pragma: no cover — import-time sanity only
    __all__ = ["WorkerManager", "WorkerProcessError"]
else:  # pragma: no cover
    __all__ = ["WorkerManager", "WorkerProcessError"]
