"""Gateway daemon lifecycle: PID/lock, stale-socket cleanup, health.

The daemon process runs :func:`serve` under ``asyncio.run``: it creates
a :class:`~orchestratord.im_gateway.gateway.MessageGateway`, opens the
UDS :class:`~orchestratord.im_gateway.ipc_server.GatewayIpcServer`
listener, writes a PID file + health file, installs SIGTERM/SIGINT
handlers for graceful shutdown, and serves until stopped.

``orchestratord gateway start|stop|status|restart`` (in
``orchestratord/cli/gateway.py``) calls :class:`GatewayDaemon`, which
spawns ``python -m orchestratord.im_gateway.server serve`` as a detached
subprocess. ``status`` reads the PID + health file; ``stop`` sends
SIGTERM. v1 acceptance is POSIX/WSL only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from orchestratord.channels.feishu_settings import FeishuAppSettings
from orchestratord.channels.models import ChannelType
from orchestratord.im_gateway.config import (
    load_config,
    migrate_legacy_state_dir,
)
from orchestratord.im_gateway.gateway import MessageGateway
from orchestratord.im_gateway.retention import run_retention_sweep
from orchestratord.utils.file_lock import HAS_FLOCK, flock_exclusive

logger = logging.getLogger(__name__)
# lark_oapi.channel imports a very large generated dispatcher. On WSL cold
# starts this can take around two minutes before the websocket timeout begins.
FEISHU_SDK_STARTUP_BUFFER_SECONDS = 150.0


@dataclass
class DaemonPaths:
    state_dir: Path
    pid_file: Path
    lock_file: Path
    sock_file: Path
    health_file: Path
    log_file: Path

    @classmethod
    def for_state_dir(cls, state_dir: str | Path | None = None) -> DaemonPaths:
        if state_dir is None:
            # Move a pre-rename ~/.orchestratord/im-gateway install forward the
            # first time the default path is resolved.
            base = migrate_legacy_state_dir()
        else:
            base = Path(state_dir).expanduser()
        base.mkdir(parents=True, exist_ok=True)
        if state_dir is None:
            # The default socket must match ``orchestratord.paths.GATEWAY_SOCK``
            # so ``orchestratord server start --gateway`` connects out of the
            # box. Resolution priority mirrors the orchestrator-side client
            # convention (cli/server.py ``_resolve_gateway_sock``): explicit
            # ``--state-dir`` wins, then ORCHESTRATORD_GATEWAY_SOCK /
            # ORCHESTRATORD_IM_GATEWAY_SOCK env overrides, then the canonical
            # GATEWAY_SOCK (~/.orchestratord/gateway/gateway.sock — the same
            # file as ``<default state dir>/gateway.sock``).
            sock = _default_gateway_sock()
        else:
            sock = base / "gateway.sock"
        return cls(
            state_dir=base,
            pid_file=base / "gateway.pid",
            lock_file=base / "gateway.lock",
            sock_file=sock,
            health_file=base / "health.json",
            log_file=base / "gateway.log",
        )


def _default_gateway_sock() -> Path:
    """Resolve the default daemon socket (env override > GATEWAY_SOCK)."""
    from orchestratord.paths import GATEWAY_SOCK

    for var in ("ORCHESTRATORD_GATEWAY_SOCK", "ORCHESTRATORD_IM_GATEWAY_SOCK"):
        value = os.environ.get(var)
        if value:
            sock = Path(value).expanduser()
            sock.parent.mkdir(parents=True, exist_ok=True)
            return sock
    return GATEWAY_SOCK


# -- process helpers -----------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def read_pid(paths: DaemonPaths) -> int | None:
    if not paths.pid_file.exists():
        return None
    try:
        return int(paths.pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def write_pid(paths: DaemonPaths, pid: int) -> None:
    paths.pid_file.write_text(f"{pid}\n", encoding="utf-8")


def cleanup_stale(paths: DaemonPaths) -> bool:
    """Remove stale PID + socket if the recorded PID is dead.

    Returns True if any stale artifact was removed.
    """
    removed = False
    pid = read_pid(paths)
    if pid is not None and not is_pid_alive(pid):
        with contextlib.suppress(FileNotFoundError):
            paths.pid_file.unlink()
        removed = True
    # A socket without a live PID is stale.
    if paths.sock_file.exists() and (pid is None or not is_pid_alive(pid)):
        with contextlib.suppress(FileNotFoundError):
            paths.sock_file.unlink()
        removed = True
    return removed


def acquire_lock(paths: DaemonPaths) -> int | None:
    """Try to acquire the single-instance lock. Returns fd or None."""
    paths.lock_file.parent.mkdir(parents=True, exist_ok=True)
    paths.lock_file.touch(exist_ok=True)
    fd = os.open(str(paths.lock_file), os.O_RDWR)
    try:
        if HAS_FLOCK:
            flock_exclusive(fd, non_blocking=True)
        return fd
    except OSError:
        os.close(fd)
        return None


def write_health(paths: DaemonPaths, **fields) -> None:
    data = {
        "running": True,
        "pid": os.getpid(),
        "started_at": fields.get("started_at", time.time()),
        "channels": fields.get("channels", []),
        "channel_status": fields.get("channel_status", {}),
        "state_dir": str(paths.state_dir),
        "socket": str(paths.sock_file),
    }
    paths.health_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read_health(paths: DaemonPaths) -> dict | None:
    if not paths.health_file.exists():
        return None
    try:
        return json.loads(paths.health_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def startup_health_wait_seconds(paths: DaemonPaths) -> float:
    """CLI wait budget for daemon health after spawning the child process."""
    try:
        config = load_config(paths.state_dir / "channels.yaml")
    except Exception:  # noqa: BLE001
        return 45.0
    timeout = 0.0
    for channel in config.channels:
        if not channel.enabled or channel.type is not ChannelType.FEISHU:
            continue
        extra = channel.extra or {}
        mode = str(extra.get("connection_mode") or "websocket").lower()
        if mode != "websocket":
            continue
        settings = FeishuAppSettings.from_config(channel)
        timeout = max(timeout, settings.startup_connect_timeout_seconds)
    if timeout <= 0.0:
        return 45.0
    return timeout + FEISHU_SDK_STARTUP_BUFFER_SECONDS


def _channel_status_ready(status: object) -> bool:
    from orchestratord.channels.health import channel_status_ready

    return channel_status_ready(status)


def _channel_status_retrying(status: object) -> bool:
    return str(status) == "websocket:retrying"


# -- daemon process ------------------------------------------------------


def _resolve_log_level(verbose: bool, env: str | None) -> int:
    """Pick the daemon log level.

    Priority: ``ORCHESTRATORD_GATEWAY_LOG_LEVEL`` env >
    ``ORCHESTRATORD_DEBUG=1`` (DEBUG) > ``--verbose`` flag (DEBUG) >
    default (WARNING). Pass ``--verbose`` or set ``ORCHESTRATORD_DEBUG=1``
    during diagnosis; set ``ORCHESTRATORD_GATEWAY_LOG_LEVEL`` to pin a
    specific level.
    """
    if env:
        env = env.strip().upper()
        levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
        }
        if env in levels:
            return levels[env]
    if os.environ.get("ORCHESTRATORD_DEBUG", "").lower() in ("1", "true", "yes"):
        return logging.DEBUG
    if verbose:
        return logging.DEBUG
    return logging.WARNING


def _warm_feishu_sdk() -> None:
    """Trigger the slow ``import lark_oapi.channel`` in a worker thread.

    ``lark_oapi.channel`` pulls in the generated event dispatcher (every API
    processor), a multi-second import on a cold cache. Running it here (before
    the adapter constructs its ``FeishuChannel``) overlaps it with daemon
    startup so the WS connect isn't serialized behind the import. No-op if the
    SDK isn't installed.
    """
    try:
        import lark_oapi.channel  # noqa: F401 — import side effect is the point
    except Exception:
        # Pre-warm is best-effort; the connect loop will surface real errors.
        logger.debug("feishu sdk pre-warm import failed", exc_info=True)


async def serve(paths: DaemonPaths, *, log_level: int = logging.WARNING) -> int:
    """Run the gateway daemon (called from the spawned subprocess)."""
    # Configure logging FIRST with a RotatingFileHandler (10 MiB per file,
    # keep up to 3 backups so the log never exceeds ~40 MiB). The subprocess
    # stderr is still redirected to the same log file by GatewayDaemon.start,
    # capturing any unhandled crash dumps that bypass the logging framework.
    os.makedirs(paths.log_file.parent, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(log_level)
    handler = logging.handlers.RotatingFileHandler(
        str(paths.log_file),
        maxBytes=10 * 1024 * 1024,  # 10 MiB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(log_level)
    # Replace any existing handlers so the daemon subprocess owns its logging.
    root.handlers.clear()
    root.addHandler(handler)
    # Channel + gateway internals follow the resolved level; keep noisy libs
    # at WARNING so urllib/asyncio don't flood the log.
    logging.getLogger("orchestratord.channels").setLevel(log_level)
    logging.getLogger("orchestratord.im_gateway").setLevel(log_level)
    # The lark_oapi SDK forces its own logger to INFO in Client.__init__,
    # which floods the log with WS connect/ping INFO lines even when the user
    # didn't pass --verbose. Pin it to the resolved level so non-verbose runs
    # stay quiet (WARNING+), and --verbose still gets SDK DEBUG detail.
    logging.getLogger("Lark").setLevel(log_level)
    logging.getLogger("lark_oapi").setLevel(log_level)
    logging.getLogger("websockets").setLevel(log_level)

    fd = acquire_lock(paths)
    if fd is None:
        print("error: another gateway daemon holds the lock", file=sys.stderr)
        return 1
    cleanup_stale(paths)

    # Pre-warm the Feishu SDK import in a background thread. ``import
    # lark_oapi.api.*`` is a ~75s synchronous import of a large auto-generated
    # package; doing it lazily inside the adapter's connect loop delays WS
    # connect by the same amount. Kicking it off here (before gateway.start)
    # overlaps the import with the rest of daemon startup so the channel comes
    # up as soon as the import + WS handshake finish.
    import threading

    from orchestratord.channels.feishu_sdk import feishu_dependencies_available

    if feishu_dependencies_available():
        threading.Thread(target=_warm_feishu_sdk, daemon=True, name="feishu-sdk-warm").start()

    # Write the PID file BEFORE starting channel adapters. A slow / hanging /
    # crashing adapter start (e.g. the Feishu WS connect loop) used to block
    # `await gateway.start()` below, so write_pid was never reached and the
    # daemon became invisible to `stop()`/`restart()` — leaving an orphaned
    # process holding the flock. Recording the PID up front guarantees the
    # daemon is always stoppable even when an adapter start misbehaves.
    started_at = time.time()
    write_pid(paths, os.getpid())

    try:
        config = load_config(paths.state_dir / "channels.yaml")
        # Force the gateway's reliability store to live under the daemon's
        # state_dir (the YAML default points at ~/.orchestratord/gateway).
        config.state_dir = str(paths.state_dir)
        # config_path lets ``reload_channel`` re-read this same file from
        # disk instead of the boot-time in-memory copy.
        gateway = MessageGateway(config, config_path=paths.state_dir / "channels.yaml")
        await gateway.start()
        # Adapter.start() performs the blocking initial connection attempt.
        # Once MessageGateway.start() returns, collect a single status snapshot:
        # connected channels are ready, retrying channels are degraded but have
        # already been queued for background reconnect.
        channel_status = await gateway.wait_channels_ready(timeout=0.0)
        for cid, status in channel_status.items():
            if _channel_status_ready(status):
                logger.info("gateway channel ready: %s -> %s", cid, status)
            elif _channel_status_retrying(status):
                logger.warning(
                    "gateway channel degraded after initial connect failure: %s -> %s "
                    "(retrying in background; see log)",
                    cid,
                    status,
                )
            else:
                logger.warning(
                    "gateway channel NOT ready after startup window: %s -> %s "
                    "(degraded; see log; messages may be dropped until it connects)",
                    cid,
                    status,
                )
        # Register the unbound-origin handler so authorized messages get explicit
        # REPL/orchestrator connection guidance instead of being silently dropped.
        from orchestratord.im_gateway.stub_agent import make_stub_handler

        gateway.set_handler(make_stub_handler(gateway.outbound))
        logger.info("gateway inbound handler registered: unbound guidance handler")

        # Open the GatewayIpcServer UDS listener (register/heartbeat/deliver/ack
        # + control reload/status).
        from orchestratord.im_gateway.ipc_server import GatewayIpcServer

        server = GatewayIpcServer(paths.sock_file, gateway)
        await server.start()

        # Wire the opt-in push path: when an origin is bound to a REPL/orchestrator
        # peer, the dispatcher pushes inbound messages over IPC instead of the
        # stub handler. The push callback delegates to the IPC server.
        async def _push_to_opt_in(message) -> bool:
            return await server.push_deliver(
                origin=message.origin,
                delivery_id=message.message_id,
                text=message.text,
                semantic=message.semantic.value if message.semantic else None,
                context_token=message.context_token,
            )

        gateway.set_push_handler(_push_to_opt_in)
        logger.info("gateway opt-in push handler registered")

        write_health(
            paths,
            started_at=started_at,
            channels=gateway.registry.names(),
            channel_status=channel_status,
        )
    except BaseException:
        # Adapter/IPC startup failed. Remove the PID we wrote up front so the
        # next `start`/`restart` doesn't see a stale PID pointing at a dead
        # process, then release the lock and re-raise so the parent reports the
        # early exit.
        with contextlib.suppress(FileNotFoundError):
            paths.pid_file.unlink()
        os.close(fd)
        raise

    # 启动持久化文件清理定时循环(默认每 24 小时)
    retention_task = asyncio.create_task(
        _retention_loop(
            str(paths.state_dir),
            config.reliability.retention_cron_interval_seconds,
            config.reliability,
        )
    )
    logger.info(
        "gateway retention loop started: interval=%ss",
        config.reliability.retention_cron_interval_seconds,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _schedule_shutdown(sig_name: str) -> None:
        asyncio.create_task(_shutdown(server, gateway, paths, stop_event, sig_name, retention_task))

    for sig in (signal.SIGTERM, signal.SIGINT):
        sig_name = signal.Signals(sig).name
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _schedule_shutdown, sig_name)

    await stop_event.wait()
    os.close(fd)
    return 0


async def _retention_loop(state_dir: str, interval: int, reliability) -> None:
    """按 interval 周期执行 cron 清理。失败不退出,继续下一轮。"""
    while True:
        await asyncio.sleep(interval)
        try:
            run_retention_sweep(state_dir, reliability)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("im_gateway retention loop iteration failed")


async def _shutdown(
    server,
    gateway,
    paths: DaemonPaths,
    stop_event: asyncio.Event,
    sig_name: str,
    retention_task: asyncio.Task | None = None,
) -> None:
    if retention_task is not None and not retention_task.done():
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
    await server.close()
    await gateway.stop()
    with contextlib.suppress(FileNotFoundError):
        paths.pid_file.unlink()
    with contextlib.suppress(FileNotFoundError):
        paths.health_file.unlink()
    with contextlib.suppress(FileNotFoundError):
        paths.sock_file.unlink()
    stop_event.set()


# -- CLI-facing controller ----------------------------------------------


class GatewayDaemon:
    """Controls the gateway daemon subprocess from the CLI."""

    def __init__(self, paths: DaemonPaths | None = None) -> None:
        self.paths = paths or DaemonPaths.for_state_dir()

    def status(self) -> int:
        pid = read_pid(self.paths)
        if pid is None or not is_pid_alive(pid):
            print("Gateway daemon: NOT RUNNING")
            if pid is not None:
                print(f"  (stale PID {pid}; cleaned up)")
                cleanup_stale(self.paths)
            return 0
        health = read_health(self.paths) or {}
        uptime_s = int(time.time() - health.get("started_at", time.time()))
        print("Gateway daemon: RUNNING")
        print(f"  PID            : {pid}")
        print(f"  Uptime         : {uptime_s}s")
        print(f"  Socket         : {self.paths.sock_file}")
        print(f"  Log            : {self.paths.log_file}")
        print(f"  Channels       : {', '.join(health.get('channels') or []) or '(none)'}")
        print(f"  State dir      : {self.paths.state_dir}")
        return 0

    def start(self, *, verbose: bool = False) -> int:
        pid = read_pid(self.paths)
        if pid is not None and is_pid_alive(pid):
            print(f"Gateway daemon already running (PID {pid}).")
            return self.status()
        cleanup_stale(self.paths)
        # Attach the log file as the subprocess's stdout+stderr so any
        # unhandled crash dumps that bypass the logging framework are captured.
        # RotatingFileHandler inside serve() handles normal log rotation.
        log_fh = self.paths.log_file.open("a", encoding="utf-8")
        serve_args = [
            sys.executable,
            "-m",
            "orchestratord.im_gateway.server",
            "serve",
            "--state-dir",
            str(self.paths.state_dir),
        ]
        if verbose:
            serve_args.append("--verbose")
        proc = subprocess.Popen(
            serve_args,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait for the daemon to write its PID file (best-effort, bounded).
        # Then wait for the health file, which is written only after
        # ``serve()`` has waited for inbound adapters to connect — so a
        # "started" daemon has actually confirmed channel connectivity (or
        # timed out and recorded a degraded status). Total budget must exceed
        # the serve() channel-ready window (15s) plus startup overhead.
        pid_deadline = time.time() + 10.0
        new_pid: int | None = None
        while time.time() < pid_deadline:
            new_pid = read_pid(self.paths)
            if new_pid is not None and is_pid_alive(new_pid):
                break
            if proc.poll() is not None:
                print(
                    f"error: gateway daemon exited early (code {proc.returncode}); see {self.paths.log_file}",
                    file=sys.stderr,
                )
                return 1
            time.sleep(0.1)
        else:
            print("error: gateway daemon did not write PID in 10s", file=sys.stderr)
            return 1

        # Wait for the health file so we can report per-channel connectivity.
        # Feishu performs a blocking SDK import + initial websocket connection
        # inside adapter.start(), so this follows the configured startup timeout
        # plus the measured cold-import/bootstrap buffer.
        health_deadline = time.time() + startup_health_wait_seconds(self.paths)
        health = None
        while time.time() < health_deadline:
            if proc.poll() is not None:
                print(
                    f"error: gateway daemon exited early (code {proc.returncode}); see {self.paths.log_file}",
                    file=sys.stderr,
                )
                return 1
            health = read_health(self.paths)
            if health is not None:
                break
            time.sleep(0.3)

        print(f"Gateway daemon started · pid {new_pid}")
        channel_status = (health or {}).get("channel_status") or {}
        if channel_status:
            for cid, status in channel_status.items():
                if _channel_status_ready(status):
                    print(f"  channel {cid}: {status}")
                elif _channel_status_retrying(status):
                    print(
                        f"  channel {cid}: {status} — initial connect failed; "
                        f"retrying in background; see {self.paths.log_file}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"  channel {cid}: NOT connected ({status}) — see "
                        f"{self.paths.log_file}; messages may be dropped",
                        file=sys.stderr,
                    )
        elif health is None:
            print(
                f"  warning: daemon still starting (no health yet); see {self.paths.log_file}",
                file=sys.stderr,
            )
        return 0

    def stop(self, *, timeout: float = 5.0) -> int:
        pid = read_pid(self.paths)
        if pid is None or not is_pid_alive(pid):
            print("Gateway daemon: already stopped")
            cleanup_stale(self.paths)
            return 0
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            cleanup_stale(self.paths)
            return 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not is_pid_alive(pid):
                break
            time.sleep(0.1)
        if is_pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        cleanup_stale(self.paths)
        print("Gateway daemon stopped.")
        return 0

    def restart(self, *, verbose: bool = False) -> int:
        rc_stop = self.stop()
        if rc_stop != 0:
            return rc_stop
        return self.start(verbose=verbose)


# -- entry point ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestratord.im_gateway.server")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_p = sub.add_parser("serve", help="run the daemon (foreground)")
    serve_p.add_argument("--state-dir", default=None)
    serve_p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable DEBUG-level IM logging (default: WARNING; use --verbose or set "
        "ORCHESTRATORD_GATEWAY_LOG_LEVEL=INFO|DEBUG)",
    )
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        paths = DaemonPaths.for_state_dir(args.state_dir)
        log_level = _resolve_log_level(args.verbose, os.environ.get("ORCHESTRATORD_GATEWAY_LOG_LEVEL"))
        try:
            return asyncio.run(serve(paths, log_level=log_level))
        except KeyboardInterrupt:
            return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
