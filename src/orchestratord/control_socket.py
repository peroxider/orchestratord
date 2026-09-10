"""Local control transport for live control of an agent run.

Path: ``{workspace}/.run_control/{run_id}.sock``. The socket accepts
multiple concurrent clients. Incoming lines are newline-delimited JSON
``ControlCommand`` objects. Outgoing lines (via ``send_event``) are
``EventFrame`` JSON.

**Scope:**
  * ``ControlCommand.cmd`` ∈ {pause, resume, inject, stop, detach, takeover}
  * ``inject`` / ``detach`` are parsed but the agent side is a no-op for
    now (Phase 2 wires ``inject`` to ``operator_hints.md``; ``detach`` is
    a Phase 3 hook).
  * No auth: workspace filesystem permissions are the only gate.
  * Unix uses a Unix-domain socket. Windows uses a randomly assigned
    loopback TCP port; neither transport is reachable from the network.
  * Long content (transcript, large tool outputs) does NOT flow over
    this socket — that lives in ``transcript.jsonl``. The socket carries
    small control + small event frames only (typical < 1 KB).

The module-level :func:`send_cmd` is the canonical one-shot client for
the Phase 1 protocol — used by CLI tools (takeover, inject, etc.) that
need to send a single control command without keeping a long-lived
connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket as socket_module
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import AsyncIterator, Literal

logger = logging.getLogger(__name__)

ControlCmd = Literal["pause", "resume", "inject", "stop", "detach", "takeover", "flush_transcript", "followup"]


@dataclass
class ControlCommand:
    """A control command received from a socket client.

    ``cmd`` is the verb (pause / resume / inject / stop / detach /
    takeover / flush_transcript). ``payload`` is an opaque string whose
    meaning depends on the verb: for ``resume`` it overrides the agent's
    next prompt, for ``inject`` it is a free-form hint, for the others
    it is ignored.
    """

    cmd: ControlCmd
    payload: str = ""


@dataclass
class EventFrame:
    """An event broadcast to all connected clients.

    ``type`` is the event class name (TextDelta / ToolCallEvent /
    ToolResultEvent / TurnComplete / PhaseComplete / SessionComplete).
    ``data`` is the JSON-safe payload. ``ts`` is the wall-clock emit
    time (seconds since epoch).
    """

    type: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class ControlSocket:
    """Bidirectional control via a local Unix socket or loopback TCP.

    Lifecycle: ``start()`` → ``poll_commands()`` / ``send_event()`` →
    ``stop()``. ``start()`` is idempotent: a stale socket file from a
    previous unclean shutdown is unlinked best-effort. ``stop()`` is
    idempotent: closing twice does not raise.

    Concurrency: ``start()`` must be called from the same event loop
    that will drive ``poll_commands()`` and ``send_event()``. The class
    holds no thread-local state; cross-loop reuse is undefined.

    Failure handling: every public method except ``start()`` swallows
    exceptions and logs them. A broken socket must never kill the
    agent — the agent runner wraps each call site in its own
    try/except as well.
    """

    def __init__(self, sock_path: Path | None = None, *, tcp: bool = False) -> None:
        if not tcp and sock_path is None:
            raise ValueError("sock_path is required for Unix-domain transport")
        self._path = Path(sock_path) if sock_path is not None else None
        self._tcp = tcp
        self._endpoint: str | None = None
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        # Per-connection read-loop tasks. Tracked so ``stop()`` can
        # cancel them; otherwise they would leak and the process
        # would hang on shutdown.
        self._read_tasks: set[asyncio.Task[None]] = set()
        self._command_queue: asyncio.Queue[ControlCommand] = asyncio.Queue()
        self._stopped = False
        self._stale_unlinked = False

    @property
    def endpoint(self) -> str:
        """Stable discovery value consumed by local control clients."""
        if self._endpoint is None:
            raise RuntimeError("control socket has not been started")
        return self._endpoint

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start listening. Unlinks a stale socket file first.

        Raises ``OSError`` if the bind fails (e.g. permission denied on
        the parent directory). Callers should treat this as fatal for
        the socket but non-fatal for the agent — wrap in try/except
        and set ``session.control_socket = None`` on failure.
        """
        if self._tcp:
            await self._start_tcp_server()
            return

        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and not self._stale_unlinked:
            try:
                self._path.unlink()
                self._stale_unlinked = True
            except OSError as exc:
                logger.warning(
                    "control_socket: could not unlink stale socket %s: %s",
                    self._path,
                    exc,
                )
        try:
            self._server = await asyncio.start_unix_server(
                self._on_client_connected,
                path=str(self._path),
            )
            self._endpoint = str(self._path)
        except OSError as exc:
            # macOS limits sockaddr_un.sun_path to roughly 104 bytes. Long
            # workspaces are valid, so losing Pause/Resume/Stop merely because
            # their derived socket path is too long is not acceptable. Keep
            # the same local-only trust boundary by falling back to an
            # ephemeral loopback TCP listener. Other bind errors retain their
            # existing failure semantics.
            if "path too long" not in str(exc).lower():
                raise
            logger.info(
                "control_socket: Unix path is too long; using loopback TCP: %s",
                self._path,
            )
            self._tcp = True
            await self._start_tcp_server()

    async def _start_tcp_server(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client_connected, host="127.0.0.1", port=0
        )
        port = self._server.sockets[0].getsockname()[1]
        self._endpoint = f"tcp://127.0.0.1:{port}"

    async def stop(self) -> None:
        """Stop listening and remove the socket file. Idempotent."""
        self._stopped = True
        if self._server is not None:
            self._server.close()
        closed_writers = list(self._clients)
        for w in closed_writers:
            try:
                w.close()
            except Exception:
                pass
        for w in closed_writers:
            try:
                await w.wait_closed()
            except Exception:
                pass
        self._clients.clear()
        if self._server is not None:
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for t in list(self._read_tasks):
            if not t.done():
                t.cancel()
        for t in list(self._read_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._read_tasks.clear()
        if self._path is not None:
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError as exc:
                logger.warning(
                    "control_socket: could not unlink %s on stop: %s",
                    self._path,
                    exc,
                )

    # ------------------------------------------------------------------
    # Inbound: control commands
    # ------------------------------------------------------------------

    async def poll_commands(self) -> AsyncIterator[ControlCommand]:
        """Yield commands as they arrive.

        The iterator polls an internal ``asyncio.Queue`` with a 0.5s
        timeout; on timeout it loops back to the ``while not
        self._stopped`` check so ``stop()`` can terminate the
        iterator promptly. The 0.5s timeout is the upper bound on
        shutdown latency.
        """
        while not self._stopped:
            try:
                cmd = await asyncio.wait_for(
                    self._command_queue.get(),
                    timeout=0.5,
                )
                yield cmd
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Outbound: event broadcast
    # ------------------------------------------------------------------

    async def send_event(self, event: dict | EventFrame) -> None:
        """Broadcast an event to all connected clients as one JSON line.

        No-op if no clients are connected. Dead clients (write raises)
        are silently dropped from the client set; the remaining
        clients still receive the event.
        """
        if not self._clients:
            # A successfully connected client can still be waiting for the
            # server's accept callback to run.  Yield once before declaring
            # the broadcast a no-op so the first lifecycle frame is not lost
            # to that event-loop scheduling race.
            await asyncio.sleep(0)
            if not self._clients:
                return
        if isinstance(event, EventFrame):
            frame = asdict(event)
        else:
            frame = dict(event)
            frame.setdefault("ts", time.time())
        line = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        dead: list[asyncio.StreamWriter] = []
        for w in self._clients:
            try:
                w.write(line)
                await w.drain()
            except Exception:
                dead.append(w)
        for w in dead:
            self._clients.discard(w)
            try:
                w.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Sync connection callback.

        We register the writer in ``_clients`` immediately (before the
        accept task yields) so a client that connects and immediately
        writes a command will see the writer registered. If we used a
        coroutine callback, the registration would happen after the
        first ``await`` in the read loop, racing with the client's
        first ``send_event`` call.

        The connection is gated by :meth:`_peer_authorized`: a peer that
        fails the local identity check (e.g. another user on a shared
        machine) is rejected up front and never joins ``_clients``, so
        it cannot pause/stop/steal a run.
        """
        if not self._peer_authorized(writer):
            logger.warning(
                "control_socket: rejecting unauthorized peer on %s",
                self._path or self._endpoint,
            )
            try:
                writer.close()
            except Exception:
                pass
            return
        self._clients.add(writer)
        task = asyncio.create_task(self._read_loop(reader, writer))
        self._read_tasks.add(task)
        task.add_done_callback(self._read_tasks.discard)

    def _peer_authorized(self, writer: asyncio.StreamWriter) -> bool:
        """Verify the connecting peer is trusted to drive this run.

        Unix sockets use ``SO_PEERCRED``: the connecting process must
        run under the same UID as the daemon (rejects other users on a
        shared host — the historical "any local process can
        stop/pause/steal" hole). Same-process clients (the dashboard's
        in-process ChatGateway) naturally pass because their PID is the
        daemon's own.

        Loopback TCP fallback (long Unix paths / Windows) keeps its
        existing trust boundary: bound to 127.0.0.1 only, on a random
        ephemeral port whose endpoint is discoverable only through the
        per-workspace ``.endpoint.json`` file. ``SO_PEERCRED`` is not
        available for TCP, so those connections remain permitted.

        When the transport socket cannot be inspected (fake writers in
        unit tests, non-Unix transports), the peer is accepted — the
        check is best-effort and must never break legitimate clients.
        """
        sock = writer.get_extra_info("socket")
        if sock is None:
            return True
        family = getattr(sock, "family", None)
        if family != socket_module.AF_UNIX:
            return True
        try:
            # SO_PEERCRED → struct { pid_t pid; uid_t uid; gid_t gid; }
            creds = sock.getsockopt(
                socket_module.SOL_SOCKET,
                socket_module.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", creds)
        except (OSError, struct.error):
            logger.warning(
                "control_socket: SO_PEERCRED unavailable on %s — rejecting",
                self._path or self._endpoint,
            )
            return False
        return uid == os.getuid()

    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Per-connection read loop: newline-delimited JSON commands."""
        try:
            while True:
                try:
                    line = await reader.readline()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break  # client disconnected abruptly
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    logger.warning(
                        "control_socket: malformed command on %s: %s",
                        self._path or self._endpoint,
                        exc,
                    )
                    continue
                if not isinstance(payload, dict):
                    logger.warning(
                        "control_socket: ignoring non-dict command: %r",
                        payload,
                    )
                    continue
                try:
                    cmd = ControlCommand(
                        cmd=payload["cmd"],
                        payload=str(payload.get("payload", "")),
                    )
                except (KeyError, TypeError) as exc:
                    logger.warning(
                        "control_socket: invalid command shape: %s",
                        exc,
                    )
                    continue
                await self._command_queue.put(cmd)
        finally:
            self._clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass


# ----------------------------------------------------------------------
# Client-side helper
# ----------------------------------------------------------------------


async def send_cmd(
    writer: asyncio.StreamWriter,
    verb: str,
    payload: str = "",
) -> None:
    """Send one newline-delimited JSON control command to a ControlSocket.

    This is the canonical one-shot client for the Phase 1 protocol.
    Used by CLI tools (takeover, inject, pause/resume/stop) that open a
    short-lived connection, send one or more commands, then close.
    """
    writer.write(
        (json.dumps({"cmd": verb, "payload": payload}) + "\n").encode("utf-8"),
    )
    await writer.drain()
