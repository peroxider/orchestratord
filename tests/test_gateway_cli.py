"""Tests for the Gateway daemon lifecycle (PID/lock/stale socket/health),
the ``orchestratord gateway`` CLI routing, and the channel wizard.

Migrated from ClawCodex ``test_gateway_cmd.py`` + ``test_channels_cmd.py``
(no external-service parts). The daemon subprocess smoke test needs
POSIX UDS and runs in WSL.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import pytest

from orchestratord.channels.models import ChannelConfig, ChannelType
from orchestratord.cli import gateway as gw
from orchestratord.cli.gateway import add_gateway_parser, run
from orchestratord.im_gateway.server import (
    DaemonPaths,
    GatewayDaemon,
    acquire_lock,
    cleanup_stale,
    is_pid_alive,
    read_pid,
)
from orchestratord.ipc.models import IM_DIRECT_ALL_ORIGIN, WECHAT_DIRECT_ALL_ORIGIN

# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _gateway_cli(argv: list[str]) -> int:
    """Parse ``orchestratord gateway ...`` argv exactly like cli/main.py."""
    parser = argparse.ArgumentParser(prog="orchestratord")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    add_gateway_parser(subparsers)
    args = parser.parse_args(["gateway", *argv])
    return run(args)


@pytest.fixture
def daemon_factory(tmp_path):
    """Create GatewayDaemon instances that are always stopped on teardown."""
    daemons: list[GatewayDaemon] = []

    def _make(state_dir=None) -> GatewayDaemon:
        daemon = GatewayDaemon(DaemonPaths.for_state_dir(state_dir or tmp_path))
        daemons.append(daemon)
        return daemon

    yield _make
    for daemon in daemons:
        daemon.stop()


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


def test_status_not_running(tmp_path) -> None:
    daemon = GatewayDaemon(DaemonPaths.for_state_dir(tmp_path))
    rc = daemon.status()
    assert rc == 0  # not-running is a valid status


def test_stale_socket_cleanup(tmp_path) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    # stale PID pointing at a dead process + a leftover socket
    paths.pid_file.write_text("999999\n", encoding="utf-8")
    paths.sock_file.write_text("", encoding="utf-8")
    assert cleanup_stale(paths) is True
    assert not paths.pid_file.exists()
    assert not paths.sock_file.exists()


def test_stale_socket_kept_when_pid_alive(tmp_path) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    paths.pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    paths.sock_file.write_text("", encoding="utf-8")
    # current process is alive → not stale
    assert cleanup_stale(paths) is False
    assert paths.pid_file.exists()


def test_acquire_lock_single_instance(tmp_path) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    fd1 = acquire_lock(paths)
    assert fd1 is not None
    fd2 = acquire_lock(paths)
    assert fd2 is None  # already locked
    os.close(fd1)


def test_is_pid_alive() -> None:
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(999999) is False
    assert is_pid_alive(0) is False


def test_daemon_start_status_stop_smoke(daemon_factory, tmp_path) -> None:
    """Start the real daemon subprocess, verify PID/socket/health, stop it.

    Needs subprocess + POSIX UDS (run in WSL). The fixture guarantees the
    daemon is stopped even when an assertion fails mid-flight.
    """
    daemon = daemon_factory(tmp_path)
    rc = daemon.start()
    assert rc == 0, (
        f"daemon failed to start; log:\n{daemon.paths.log_file.read_text(encoding='utf-8')[-4000:]}"
    )
    pid = read_pid(daemon.paths)
    assert pid is not None and is_pid_alive(pid)
    assert daemon.paths.sock_file.exists()
    # health file written
    health = daemon.paths.health_file.read_text(encoding="utf-8")
    assert '"running": true' in health
    assert daemon.stop() == 0
    # after stop, cleaned up
    assert read_pid(daemon.paths) is None or not is_pid_alive(read_pid(daemon.paths))
    assert not daemon.paths.sock_file.exists()


# ---------------------------------------------------------------------------
# DaemonPaths socket resolution
# ---------------------------------------------------------------------------


def test_daemon_paths_default_sock_matches_gateway_sock(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ORCHESTRATORD_GATEWAY_SOCK", raising=False)
    monkeypatch.delenv("ORCHESTRATORD_IM_GATEWAY_SOCK", raising=False)
    paths = DaemonPaths.for_state_dir(tmp_path)
    assert paths.sock_file == tmp_path / "gateway.sock"


def test_daemon_paths_default_resolves_gateway_sock(monkeypatch, tmp_path) -> None:
    """With no --state-dir, the default socket is orchestratord.paths.GATEWAY_SOCK."""
    monkeypatch.delenv("ORCHESTRATORD_GATEWAY_SOCK", raising=False)
    monkeypatch.delenv("ORCHESTRATORD_IM_GATEWAY_SOCK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    from orchestratord.paths import GATEWAY_SOCK

    paths = DaemonPaths.for_state_dir(None)

    # The socket is the canonical GATEWAY_SOCK constant (resolved from
    # orchestratord.paths at import time), so the daemon default always
    # matches what `orchestratord server start --gateway` connects to.
    assert paths.sock_file == GATEWAY_SOCK
    # The state dir follows the (legacy-migrated) HOME-based default.
    assert paths.state_dir == tmp_path / ".orchestratord" / "gateway"


def test_daemon_paths_env_sock_overrides_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ORCHESTRATORD_IM_GATEWAY_SOCK", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    env_sock = tmp_path / "custom" / "other.sock"
    monkeypatch.setenv("ORCHESTRATORD_GATEWAY_SOCK", str(env_sock))

    paths = DaemonPaths.for_state_dir(None)

    assert paths.sock_file == env_sock
    assert env_sock.parent.exists()


def test_daemon_paths_explicit_state_dir_wins_over_env(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state"
    env_sock = tmp_path / "env2.sock"
    monkeypatch.setenv("ORCHESTRATORD_GATEWAY_SOCK", str(tmp_path / "env.sock"))
    monkeypatch.setenv("ORCHESTRATORD_IM_GATEWAY_SOCK", str(env_sock))

    paths = DaemonPaths.for_state_dir(state_dir)

    assert paths.sock_file == state_dir / "gateway.sock"
    assert paths.state_dir == state_dir


# ---------------------------------------------------------------------------
# serve() lifecycle invariants
# ---------------------------------------------------------------------------


def test_serve_writes_pid_before_gateway_start(tmp_path, monkeypatch) -> None:
    """PID file must be written BEFORE channel adapters start.

    A hanging / crashing adapter start used to block ``await gateway.start()``
    so ``write_pid`` was never reached — the daemon became invisible to
    ``stop()``/``restart()`` and left an orphan holding the flock. Asserting
    the PID exists even when ``gateway.start`` raises proves the ordering.
    """
    import asyncio

    from orchestratord.im_gateway import server as srv

    paths = DaemonPaths.for_state_dir(tmp_path)
    seen_pid_at_start: list[int | None] = []

    class _CrashingGateway:
        def __init__(self, *a, **kw) -> None:
            pass

        async def start(self) -> None:
            # If write_pid ran before us, the PID file already exists.
            seen_pid_at_start.append(read_pid(paths))
            raise RuntimeError("adapter start crashed")

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(srv, "MessageGateway", _CrashingGateway)
    # load_config needs a config file; write a minimal one.
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "channels.yaml").write_text(
        "enabled: true\nchannels: []\n", encoding="utf-8"
    )

    # gateway.start raises → serve propagates the RuntimeError.
    with pytest.raises(RuntimeError, match="adapter start crashed"):
        asyncio.run(srv.serve(paths, log_level=40))  # CRITICAL = quiet

    # The PID file was already written when gateway.start ran (proving
    # write_pid precedes adapter start), then cleaned up on the failure path.
    assert seen_pid_at_start == [os.getpid()]
    assert seen_pid_at_start[0] is not None
    assert not paths.pid_file.exists()


def test_gateway_start_reports_retrying_channel_as_degraded_success(
    tmp_path, monkeypatch, capsys
) -> None:
    from orchestratord.im_gateway import server as srv

    paths = DaemonPaths.for_state_dir(tmp_path)
    paths.log_file.write_text("", encoding="utf-8")

    class _FakeProc:
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(srv.subprocess, "Popen", lambda *a, **kw: _FakeProc())
    read_pid_values = iter([None, 12345])
    monkeypatch.setattr(srv, "read_pid", lambda _paths: next(read_pid_values, 12345))
    monkeypatch.setattr(srv, "is_pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(
        srv,
        "read_health",
        lambda _paths: {
            "started_at": time.time(),
            "channels": ["feishu"],
            "channel_status": {"feishu": "websocket:retrying"},
        },
    )
    monkeypatch.setattr(srv, "startup_health_wait_seconds", lambda _paths: 0.1)

    rc = GatewayDaemon(paths).start()
    captured = capsys.readouterr()

    assert rc == 0
    assert "Gateway daemon started" in captured.out
    assert "channel feishu: websocket:retrying" in captured.err
    assert "retrying in background" in captured.err
    assert "NOT connected" not in captured.err
    assert "messages may be dropped" not in captured.err


def test_startup_health_wait_seconds_includes_feishu_sdk_import_buffer(tmp_path) -> None:
    from orchestratord.im_gateway import server as srv

    paths = DaemonPaths.for_state_dir(tmp_path)
    (paths.state_dir / "channels.yaml").write_text(
        "enabled: true\n"
        "channels:\n"
        "  - type: feishu\n"
        '    webhook_url: ""\n'
        "    name: feishu\n"
        "    enabled: true\n"
        "    extra:\n"
        "      connection_mode: websocket\n"
        "      app_id: cli_app\n"
        "      app_secret: secret\n"
        "      websocket:\n"
        "        startup_connect_timeout_seconds: 7.5",
        encoding="utf-8",
    )

    assert srv.startup_health_wait_seconds(paths) == pytest.approx(157.5)


# ---------------------------------------------------------------------------
# CLI routing
# ---------------------------------------------------------------------------


def test_gateway_no_args_errors_with_usage(capsys) -> None:
    """`gateway` (no args) is rejected by argparse with the usage text."""
    with pytest.raises(SystemExit) as exc_info:
        _gateway_cli([])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "usage:" in err


def test_gateway_help_prints_usage(capsys) -> None:
    """`gateway --help` prints usage and exits 0."""
    with pytest.raises(SystemExit) as exc_info:
        _gateway_cli(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "start" in out and "setup" in out and "login" in out


def test_gateway_unknown_subcommand_errors(capsys) -> None:
    """`gateway <unknown>` is rejected by argparse."""
    with pytest.raises(SystemExit) as exc_info:
        _gateway_cli(["bogus"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err


def test_gateway_channels_verb_is_unknown(capsys) -> None:
    """`gateway channels status` is invalid — channels are flattened verbs."""
    with pytest.raises(SystemExit) as exc_info:
        _gateway_cli(["channels", "status"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err


def test_gateway_wizard_verb_is_unknown(capsys) -> None:
    """`gateway wizard` is invalid — only `setup` runs the wizard."""
    with pytest.raises(SystemExit) as exc_info:
        _gateway_cli(["wizard"])
    assert exc_info.value.code == 2


def test_gateway_start_with_name_errors(capsys) -> None:
    """`gateway start <name>` is invalid — start takes no channel name."""
    rc = _gateway_cli(["start", "wechat"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "start takes no channel name" in err.lower()


def test_gateway_stop_with_name_errors(capsys) -> None:
    """`gateway stop <name>` is invalid — stop takes no channel name."""
    rc = _gateway_cli(["stop", "wechat"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "stop takes no channel name" in err.lower()


def test_gateway_setup_uses_state_dir_then_restarts_daemon(tmp_path, monkeypatch) -> None:
    """A successful setup writes the selected config and applies it via restart."""
    wizard_calls: list[str | None] = []
    restart_calls: list[bool] = []

    def _fake_wizard(path: str | None = None, *, input_fn=None) -> int:
        wizard_calls.append(path)
        return 0

    class _FakeDaemon:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def restart(self, verbose=False):
            restart_calls.append(verbose)
            return 0

    monkeypatch.setattr(gw, "run_wizard", _fake_wizard)
    monkeypatch.setattr("orchestratord.im_gateway.server.GatewayDaemon", _FakeDaemon)
    rc = _gateway_cli(["setup", "--state-dir", str(tmp_path), "--verbose"])
    assert rc == 0
    assert wizard_calls == [str(tmp_path / "channels.yaml")]
    assert restart_calls == [True]


def test_gateway_setup_failure_does_not_restart(monkeypatch) -> None:
    restart_calls: list[bool] = []

    class _FakeDaemon:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def restart(self, verbose=False):
            restart_calls.append(verbose)
            return 0

    monkeypatch.setattr(gw, "run_wizard", lambda _path: 1)
    monkeypatch.setattr("orchestratord.im_gateway.server.GatewayDaemon", _FakeDaemon)

    assert _gateway_cli(["setup"]) == 1
    assert restart_calls == []


def test_gateway_restart_channel(monkeypatch) -> None:
    """`gateway restart <name>` calls restart_channel."""
    calls: list[tuple] = []

    def _fake_restart(name: str, *, state_dir: str | None = None) -> int:
        calls.append((name, state_dir))
        return 0

    monkeypatch.setattr(gw, "restart_channel", _fake_restart)
    rc = _gateway_cli(["restart", "wechat"])
    assert rc == 0
    assert calls == [("wechat", None)]


def test_gateway_restart_daemon(monkeypatch) -> None:
    """`gateway restart` (no name) calls daemon.restart."""
    calls: list[bool] = []

    class _FakeDaemon:
        def __init__(self, *a, **kw):
            pass

        def restart(self, verbose=False):
            calls.append(verbose)
            return 0

    monkeypatch.setattr("orchestratord.im_gateway.server.GatewayDaemon", _FakeDaemon)
    rc = _gateway_cli(["restart"])
    assert rc == 0
    assert calls == [False]


def test_gateway_status_channel(monkeypatch, capsys) -> None:
    """`gateway status <name>` calls format_status for that name."""
    calls: list[tuple] = []

    def _fake_format_status(path=None, name=None, *, state_dir=None) -> str:
        calls.append((path, name, state_dir))
        return f"STATUS:{name}"

    monkeypatch.setattr(gw, "format_status", _fake_format_status)
    rc = _gateway_cli(["status", "wechat"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "STATUS:wechat" in out
    assert calls == [(None, "wechat", None)]


def test_gateway_status_unified(monkeypatch, capsys) -> None:
    """Bare `gateway status` prints daemon status THEN all-channels status."""
    daemon_status_calls: list[int] = []
    format_status_calls: list[tuple] = []

    class _FakeDaemon:
        def __init__(self, *a, **kw):
            pass

        def status(self):
            daemon_status_calls.append(1)
            print("DAEMON: running", end="")
            return 0

    def _fake_format_status(path=None, name=None, *, state_dir=None) -> str:
        format_status_calls.append((path, name, state_dir))
        return "CHANNELS: all"

    monkeypatch.setattr("orchestratord.im_gateway.server.GatewayDaemon", _FakeDaemon)
    monkeypatch.setattr(gw, "format_status", _fake_format_status)

    rc = _gateway_cli(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DAEMON: running" in out
    assert "CHANNELS: all" in out
    assert len(daemon_status_calls) == 1
    assert format_status_calls == [(None, None, None)]


def test_gateway_status_not_running_via_cli(capsys, tmp_path) -> None:
    """`gateway status` with no daemon exits 0 and reports not running."""
    rc = _gateway_cli(["status", "--state-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NOT RUNNING" in out
    assert "no channels configured" in out


def test_gateway_disconnect_channel(monkeypatch) -> None:
    """`gateway disconnect <name>` calls _disconnect_gateway_connection."""
    calls: list[tuple] = []

    def _fake_disconnect(name: str, *, state_dir: str | None = None) -> int:
        calls.append((name, state_dir))
        return 0

    monkeypatch.setattr(gw, "_disconnect_gateway_connection", _fake_disconnect)
    rc = _gateway_cli(["disconnect", "wechat"])
    assert rc == 0
    assert calls == [("wechat", None)]


def test_gateway_login_channel(monkeypatch) -> None:
    """`gateway login <name>` calls wechat_login."""
    calls: list[tuple] = []

    def _fake_login(name: str, *, state_dir: str | None = None) -> int:
        calls.append((name, state_dir))
        return 0

    monkeypatch.setattr(gw, "wechat_login", _fake_login)
    rc = _gateway_cli(["login", "wechat"])
    assert rc == 0
    assert calls == [("wechat", None)]


# ---------------------------------------------------------------------------
# Channel config ops
# ---------------------------------------------------------------------------


def _slack(name: str = "slack-ops", enabled: bool = False) -> ChannelConfig:
    return ChannelConfig(
        type=ChannelType.SLACK,
        webhook_url="https://hooks.example.com/services/T/B/abcdef0123456789",
        name=name,
        enabled=enabled,
    )


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_list_channels_empty(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    assert gw.list_channels(str(p)) == []


def test_add_list_remove_channel(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    listed = gw.list_channels(str(p))
    assert listed == [{"name": "s1", "type": "slack", "enabled": True}]
    assert gw.remove_channel(str(p), "s1") is True
    assert gw.list_channels(str(p)) == []


def test_remove_wechat_channel_deletes_owned_state(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    wechat_dir = tmp_path / "wechat"
    wechat_dir.mkdir()
    owned_files = [
        wechat_dir / "wechat_auth.json",
        wechat_dir / "wechat_auth.key",
        wechat_dir / "wechat_auth.json.tmp",
        wechat_dir / "wechat_pairing.json",
        wechat_dir / "wechat_pairing.json.lock",
        wechat_dir / "wechat_pairing.json.tmp",
        wechat_dir / "wechat-main_auth.json",
        wechat_dir / "wechat-main_auth.key",
        wechat_dir / "wechat-main_auth.json.tmp",
        wechat_dir / "wechat-main_pairing.json",
        wechat_dir / "wechat-main_pairing.json.lock",
        wechat_dir / "wechat-main_pairing.json.tmp",
        tmp_path / "wechat_context_tokens.json",
        tmp_path / "wechat_accounts.json",
    ]
    for path in owned_files:
        path.write_text("owned", encoding="utf-8")
    preserved_files = [
        tmp_path / "outbox.ndjson",
        tmp_path / "processed_inbound.ndjson",
        tmp_path / "dead_letter.ndjson",
        tmp_path / "audit.ndjson",
    ]
    for path in preserved_files:
        path.write_text("preserve", encoding="utf-8")

    assert gw.remove_channel(str(p), "wechat") is True

    assert gw.list_channels(str(p)) == []
    assert all(not path.exists() for path in owned_files)
    assert all(path.exists() for path in preserved_files)
    assert p.exists()


def test_remove_feishu_channel_prunes_last_sender_key(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_channel_from_inputs("feishu", "feishu", {}))
    _write_json(tmp_path / "feishu_last_senders.json", {"feishu": "ou_old", "other": "ou_keep"})

    assert gw.remove_channel(str(p), "feishu") is True

    assert json.loads((tmp_path / "feishu_last_senders.json").read_text(encoding="utf-8")) == {
        "other": "ou_keep"
    }


def test_remove_feishu_channel_deletes_empty_last_sender_file(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_channel_from_inputs("feishu", "feishu", {}))
    _write_json(tmp_path / "feishu_last_senders.json", {"feishu": "ou_old"})

    assert gw.remove_channel(str(p), "feishu") is True

    assert not (tmp_path / "feishu_last_senders.json").exists()


def test_remove_generic_channel_preserves_gateway_state(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    preserved = tmp_path / "outbox.ndjson"
    preserved.write_text("preserve", encoding="utf-8")

    assert gw.remove_channel(str(p), "s1") is True

    assert preserved.exists()


def test_add_channel_replaces_existing_channel_of_same_type(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("slack-old", enabled=False))
    gw.add_channel(str(p), _slack("slack-new", enabled=True))

    assert gw.list_channels(str(p)) == [{"name": "slack-new", "type": "slack", "enabled": True}]


def test_update_channel_replaces(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    gw.update_channel(str(p), _slack("s1", enabled=False))
    listed = gw.list_channels(str(p))
    assert listed[0]["enabled"] is False
    assert gw.update_channel(str(p), _slack("renamed")) is True
    assert gw.list_channels(str(p)) == [{"name": "renamed", "type": "slack", "enabled": False}]
    missing_type = ChannelConfig(
        type=ChannelType.DISCORD,
        webhook_url="https://discord.com/api/webhooks/1/token",
        name="discord-main",
    )
    assert gw.update_channel(str(p), missing_type) is False


def test_build_channel_from_inputs_wechat(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    channel = gw.build_channel_from_inputs(
        "wechat",
        "wechat",
        {
            "base_url": "https://ilinkai.weixin.qq.com",
            "account_id": "default",
            "enabled": "true",
        },
    )
    assert channel.type is ChannelType.WECHAT
    assert channel.enabled is True
    assert channel.extra["base_url"] == "https://ilinkai.weixin.qq.com"
    assert "allowed_users" not in channel.extra
    # round-trip via save/load
    gw.add_channel(str(p), channel)
    loaded = gw.list_channels(str(p))
    assert loaded == [{"name": "wechat", "type": "wechat", "enabled": True}]


def test_build_channel_from_inputs_feishu_websocket() -> None:
    channel = gw.build_channel_from_inputs(
        "feishu",
        "feishu",
        {
            "connection_mode": "websocket",
            "app_id": "cli_app",
            "app_secret": "secret",
            "encrypt_key": "encrypt-key",
            "verification_token": "verification-token",
            "domain": "lark",
            "allowed_user_open_id": "ou_allowed",
            "bot_open_id": "ou_bot",
            "bot_name": "orchestratord",
            "ws_reconnect_interval": "180",
            "ws_ping_interval": "",
            "ws_ping_timeout": "12",
            "enabled": "true",
        },
    )

    assert channel.type is ChannelType.FEISHU
    assert channel.webhook_url == ""
    assert channel.name == "feishu"
    assert channel.enabled is True
    assert channel.extra["connection_mode"] == "websocket"
    assert channel.extra["app_id"] == "cli_app"
    assert channel.extra["app_secret"] == "secret"
    assert channel.extra["encrypt_key"] == "encrypt-key"
    assert channel.extra["verification_token"] == "verification-token"
    assert channel.extra["domain"] == "lark"
    assert channel.extra["allowed_user_open_id"] == "ou_allowed"
    assert channel.extra["bot_open_id"] == "ou_bot"
    assert channel.extra["bot_name"] == "orchestratord"
    assert "websocket" not in channel.extra
    assert channel.extra["send"] == {
        "sdk_send_attempts": 3,
        "sdk_send_backoff_base_seconds": 1.0,
    }


def test_build_channel_from_inputs_feishu_webhook_compat() -> None:
    channel = gw.build_channel_from_inputs(
        "feishu",
        "feishu",
        {
            "connection_mode": "webhook",
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef",
            "secret": "sign_secret",
            "enabled": "true",
        },
    )

    assert channel.webhook_url == "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef"
    assert channel.extra == {"connection_mode": "webhook", "secret": "sign_secret"}


# ---------------------------------------------------------------------------
# format_status
# ---------------------------------------------------------------------------


def test_format_status(tmp_path) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    out = gw.format_status(str(p))
    assert "s1" in out and "enabled" in out
    assert "no channels" in gw.format_status(str(p), "nope")


def test_format_status_shows_connected_clients(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda state_dir=None: {
            "gateway_running": True,
            "peers": [
                {"session_id": "repl-12345", "host_type": "repl", "online": True},
                {"session_id": "orch-67890", "host_type": "orchestrator", "online": True},
            ],
        },
        raising=False,
    )

    out = gw.format_status(str(p), state_dir=str(tmp_path))

    assert "connected clients: repl (session=repl-12345, pid=12345, online)" in out
    assert "orchestrator (session=orch-67890, online)" in out


def test_format_status_shows_connected_client_pid(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda state_dir=None: {
            "gateway_running": True,
            "peers": [
                {
                    "session_id": "repl-4321-1",
                    "host_type": "repl",
                    "online": True,
                    "pid": 4321,
                },
            ],
        },
        raising=False,
    )

    out = gw.format_status(str(p), state_dir=str(tmp_path))

    assert "repl (session=repl-4321-1, pid=4321, online)" in out


def test_format_status_shows_no_clients_when_peers_empty(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda state_dir=None: {"gateway_running": True, "peers": []},
        raising=False,
    )

    out = gw.format_status(str(p), state_dir=str(tmp_path))

    assert "connected clients: none" in out


def test_format_status_omits_clients_line_when_daemon_down(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("s1", enabled=True))
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda state_dir=None: {"gateway_running": False},
        raising=False,
    )

    out = gw.format_status(str(p), state_dir=str(tmp_path))

    assert "connected clients" not in out


def test_format_status_wechat_includes_login_and_conversation_connection(
    tmp_path,
    monkeypatch,
) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    monkeypatch.setattr(
        gw,
        "wechat_login_status",
        lambda name, state_dir=None: "logged_in (account_id=acct, user_id=bot_user)",
    )
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda state_dir=None: {
            "bindings": [
                {
                    "origin": WECHAT_DIRECT_ALL_ORIGIN,
                    "session_id": "repl-main",
                    "host_type": "repl",
                    "connection_state": "active",
                }
            ],
            "peers": [
                {
                    "session_id": "repl-main",
                    "origin": WECHAT_DIRECT_ALL_ORIGIN,
                    "host_type": "repl",
                    "online": True,
                }
            ],
            "channel_health": [
                {
                    "channel_id": "wechat",
                    "healthy": True,
                    "account_status": "logged_in",
                }
            ],
        },
        raising=False,
    )

    out = gw.format_status(str(p), "wechat", state_dir=str(tmp_path))

    assert "login: logged_in" in out
    assert "conversation: connected to repl" in out
    assert "session=repl-main" in out


def test_format_status_wechat_reports_disconnected_conversation(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "unconfigured")
    monkeypatch.setattr(
        gw, "_read_gateway_runtime_status", lambda state_dir=None: {}, raising=False
    )

    out = gw.format_status(str(p), "wechat", state_dir=str(tmp_path))

    assert "conversation: disconnected" in out


def test_format_status_feishu_includes_generic_conversation_connection(
    tmp_path,
    monkeypatch,
) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_channel_from_inputs("feishu", "feishu", {}))
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda state_dir=None: {
            "bindings": [
                {
                    "origin": IM_DIRECT_ALL_ORIGIN,
                    "session_id": "repl-main",
                    "host_type": "repl",
                    "connection_state": "active",
                }
            ],
            "peers": [
                {
                    "session_id": "repl-main",
                    "origin": IM_DIRECT_ALL_ORIGIN,
                    "host_type": "repl",
                    "online": True,
                }
            ],
            "channel_health": [
                {
                    "channel_id": "feishu",
                    "healthy": True,
                    "account_status": "websocket:connected",
                }
            ],
        },
        raising=False,
    )

    out = gw.format_status(str(p), "feishu", state_dir=str(tmp_path))

    assert "conversation: connected to repl" in out
    assert "session=repl-main" in out


def test_format_status_wechat_reports_offline_binding_as_disconnected(
    tmp_path, monkeypatch
) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    monkeypatch.setattr(
        gw,
        "wechat_login_status",
        lambda name, state_dir=None: "logged_in (account_id=acct, user_id=bot_user)",
    )
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda state_dir=None: {
            "bindings": [
                {
                    "origin": WECHAT_DIRECT_ALL_ORIGIN,
                    "session_id": "repl-main",
                    "host_type": "repl",
                    "connection_state": "offline",
                }
            ],
            "peers": [
                {
                    "session_id": "repl-main",
                    "origin": WECHAT_DIRECT_ALL_ORIGIN,
                    "host_type": "repl",
                    "online": False,
                }
            ],
        },
        raising=False,
    )

    out = gw.format_status(str(p), "wechat", state_dir=str(tmp_path))

    assert "conversation: disconnected" in out
    assert "connected to repl" not in out


def test_format_status_feishu_reports_runtime_health_and_approval_support(
    tmp_path, monkeypatch
) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _feishu_ws_channel())
    monkeypatch.setattr(
        gw,
        "_read_gateway_runtime_status",
        lambda _state_dir: {
            "gateway_running": True,
            "channel_health": [
                {
                    "channel_id": "feishu",
                    "account_status": "websocket:connected",
                    "extra": {
                        "connection_mode": "websocket",
                        "domain": "feishu",
                        "approval_cards": "supported",
                    },
                }
            ],
        },
    )

    out = gw.format_status(str(p), "feishu")

    assert "mode: websocket" in out
    assert "health: websocket:connected" in out
    assert "approval_cards" not in out


# ---------------------------------------------------------------------------
# restart_channel
# ---------------------------------------------------------------------------


def test_restart_channel_reports_connected_client_pid_on_exception(
    tmp_path, monkeypatch, capsys
) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "_daemon_alive", lambda daemon: True)

    class _FakeGatewayIpcClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def status(self):
            return {
                "gateway_running": True,
                "peers": [
                    {
                        "session_id": "orchestrator-4321",
                        "host_type": "orchestrator",
                        "online": True,
                        "pid": 4321,
                    }
                ],
            }

        async def reload_channel(self, name):
            raise RuntimeError("adapter busy")

    monkeypatch.setattr(
        "orchestratord.ipc.client.GatewayIpcClient",
        _FakeGatewayIpcClient,
    )

    rc = gw.restart_channel("wechat", state_dir=str(tmp_path))
    captured = capsys.readouterr()

    assert rc == 1
    assert "restart failed" in captured.err
    assert "REPL/Orchestrator" in captured.err
    assert "pid=4321" in captured.err


def test_restart_channel_validates_config_when_daemon_down(tmp_path, monkeypatch, capsys) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "_daemon_alive", lambda daemon: False)

    rc = gw.restart_channel("wechat", state_dir=str(tmp_path))

    assert rc == 0
    captured = capsys.readouterr()
    assert "config validated" in captured.out
    assert "orchestratord gateway start" in captured.out


def test_restart_channel_unknown_name_errors(tmp_path, capsys) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))

    rc = gw.restart_channel("nope", state_dir=str(tmp_path))

    assert rc == 2
    assert "no channel named 'nope'" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# WeChat login
# ---------------------------------------------------------------------------


def test_wechat_login_status_unconfigured(tmp_path) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    out = gw.wechat_login_status("wechat", state_dir=str(tmp_path))
    assert "unconfigured" in out


def test_wechat_login_status_reuses_legacy_wechat_main_auth(tmp_path) -> None:
    from orchestratord.channels.wechat_ilink import (
        WeChatAuthRecord,
        WeChatIlinkAuthStore,
    )

    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    legacy_auth = paths.state_dir / "wechat" / "wechat-main_auth.json"
    WeChatIlinkAuthStore(legacy_auth).save(
        WeChatAuthRecord(
            bot_token="bot_tok_123",
            account_id="acct",
            base_url="https://ilinkai.weixin.qq.com",
            user_id="bot_user",
        )
    )

    out = gw.wechat_login_status("wechat", state_dir=str(tmp_path))

    assert "logged_in" in out
    assert "account_id=acct" in out


def test_wechat_login_reports_ilink_http_error_without_traceback(
    tmp_path, monkeypatch, capsys
) -> None:
    from orchestratord.channels.wechat_ilink import (
        WeChatIlinkChannelAdapter,
        _IlinkHttpError,
    )

    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))

    async def _raise_404(self, **_kwargs) -> dict:
        raise _IlinkHttpError(404, b"not found")

    monkeypatch.setattr(WeChatIlinkChannelAdapter, "qr_login", _raise_404)

    rc = gw.wechat_login("wechat", state_dir=str(tmp_path))

    captured = capsys.readouterr()
    assert rc == 1
    assert "iLink 登录失败" in captured.err
    assert "404" in captured.err
    assert "/getlogincode" not in captured.err


def test_wechat_login_prints_qr_before_waiting_and_reports_success(
    tmp_path, monkeypatch, capsys
) -> None:
    from orchestratord.channels.wechat_ilink import WeChatIlinkChannelAdapter

    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "_print_terminal_qr", lambda scan_data: None)

    async def _fake_qr_login(self, *, on_code=None, on_status=None, **_kwargs) -> dict:
        if on_code is not None:
            on_code("https://ilinkai.weixin.qq.com/qr/abc")
        if on_status is not None:
            on_status("wait")
            on_status("scaned")
        return {
            "status": "confirmed",
            "code_url": "https://ilinkai.weixin.qq.com/qr/abc",
            "bot_token": "bot_tok_123",
            "account_id": "bot_account_1",
        }

    monkeypatch.setattr(WeChatIlinkChannelAdapter, "qr_login", _fake_qr_login)
    # daemon is "running" so login takes the live-reload path (no subprocess)
    monkeypatch.setattr(gw, "_daemon_alive", lambda daemon: True)
    monkeypatch.setattr(gw, "restart_channel", lambda name, state_dir=None: 0)

    rc = gw.wechat_login("wechat", state_dir=str(tmp_path))

    captured = capsys.readouterr()
    assert rc == 0
    assert "https://ilinkai.weixin.qq.com/qr/abc" in captured.out
    assert "已扫码" in captured.out
    assert "WeChat 登录成功 (account_id=bot_account_1)" in captured.out


def test_wechat_login_reloads_channel_after_success(tmp_path, monkeypatch, capsys) -> None:
    from orchestratord.channels.wechat_ilink import WeChatIlinkChannelAdapter

    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "_print_terminal_qr", lambda scan_data: None)
    reload_calls: list[tuple[str, str | None]] = []

    async def _fake_qr_login(self, *, on_code=None, on_status=None, **_kwargs) -> dict:
        if on_code is not None:
            on_code("https://ilinkai.weixin.qq.com/qr/abc")
        if on_status is not None:
            on_status("confirmed")
        return {
            "status": "confirmed",
            "bot_token": "bot_tok_123",
            "account_id": "bot_account_1",
        }

    def _fake_restart(name: str, *, state_dir: str | None = None) -> int:
        reload_calls.append((name, state_dir))
        return 0

    monkeypatch.setattr(WeChatIlinkChannelAdapter, "qr_login", _fake_qr_login)
    monkeypatch.setattr(gw, "restart_channel", _fake_restart)
    # daemon is "running" so login takes the live-reload path
    monkeypatch.setattr(gw, "_daemon_alive", lambda daemon: True)

    rc = gw.wechat_login("wechat", state_dir=str(tmp_path))

    assert rc == 0
    assert reload_calls == [("wechat", str(tmp_path))]
    assert "WeChat 登录成功" in capsys.readouterr().out


def test_wechat_login_auto_starts_daemon_when_down(tmp_path, monkeypatch, capsys) -> None:
    """When the daemon is DOWN, login must start it (not silently succeed)."""
    from orchestratord.channels.wechat_ilink import WeChatIlinkChannelAdapter

    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "_print_terminal_qr", lambda scan_data: None)

    async def _fake_qr_login(self, *, on_code=None, on_status=None, **_kwargs) -> dict:
        return {"status": "confirmed", "bot_token": "bot_tok_123", "account_id": "bot_account_1"}

    start_calls: list[bool] = []

    class _FakeDaemon:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            start_calls.append(True)
            return 0

    monkeypatch.setattr(WeChatIlinkChannelAdapter, "qr_login", _fake_qr_login)
    monkeypatch.setattr(gw, "_daemon_alive", lambda daemon: False)
    monkeypatch.setattr("orchestratord.im_gateway.server.GatewayDaemon", _FakeDaemon)
    # restart_channel must NOT be called when daemon was down
    monkeypatch.setattr(gw, "restart_channel", lambda *a, **k: 1)

    rc = gw.wechat_login("wechat", state_dir=str(tmp_path))
    assert rc == 0
    assert start_calls == [True]
    assert "守护进程已启动" in capsys.readouterr().out


def test_wechat_login_daemon_start_failure_reports_error(tmp_path, monkeypatch, capsys) -> None:
    """When daemon.start() fails, login must surface the error (not silent success)."""
    from orchestratord.channels.wechat_ilink import WeChatIlinkChannelAdapter

    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "_print_terminal_qr", lambda scan_data: None)

    async def _fake_qr_login(self, *, on_code=None, on_status=None, **_kwargs) -> dict:
        return {"status": "confirmed", "bot_token": "bot_tok_123", "account_id": "bot_account_1"}

    class _FakeDaemon:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            return 1  # start failed

    monkeypatch.setattr(WeChatIlinkChannelAdapter, "qr_login", _fake_qr_login)
    monkeypatch.setattr(gw, "_daemon_alive", lambda daemon: False)
    monkeypatch.setattr("orchestratord.im_gateway.server.GatewayDaemon", _FakeDaemon)

    rc = gw.wechat_login("wechat", state_dir=str(tmp_path))
    assert rc == 1
    assert "启动失败" in capsys.readouterr().err


def test_disconnect_gateway_connection_when_daemon_down(tmp_path, capsys) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), gw.build_default_channel("wechat"))

    rc = gw._disconnect_gateway_connection("wechat", state_dir=str(tmp_path))

    assert rc == 0
    assert "already disconnected" in capsys.readouterr().out


def test_disconnect_gateway_connection_rejects_non_im_channel(tmp_path, capsys) -> None:
    paths = DaemonPaths.for_state_dir(tmp_path)
    gw.add_channel(str(paths.state_dir / "channels.yaml"), _slack("s1", enabled=True))

    rc = gw._disconnect_gateway_connection("s1", state_dir=str(tmp_path))

    assert rc == 2
    assert "only supported for IM app channels" in capsys.readouterr().err


def test_disconnect_gateway_connection_unknown_channel(tmp_path, capsys) -> None:
    rc = gw._disconnect_gateway_connection("nope", state_dir=str(tmp_path))

    assert rc == 2
    assert "no configured channel named 'nope'" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Wizard (interactive, input_fn-simulated)
# ---------------------------------------------------------------------------


def test_wizard_add_then_edit_then_remove(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    monkeypatch.setattr(gw, "wechat_login", lambda name, state_dir=None: 0)
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "logged_in")
    # 新流程：wechat 是第 2 项；未登录 → _wizard_add_wechat（扫码 + 编辑菜单）
    inputs = iter(
        [
            "2",  # select wechat (not logged in → add flow: scan + edit menu)
            "",  # ESC wechat edit menu → back to channel select
            "",  # ESC exit wizard
        ]
    )
    rc = gw.run_wizard(str(p), input_fn=lambda _prompt: next(inputs))
    assert rc == 0
    listed = gw.list_channels(str(p))
    assert listed == [{"name": "wechat", "type": "wechat", "enabled": True}]

    # edit: disable then remove（wechat 已登录 → 编辑菜单；选项 4=启停，5=移除）
    inputs2 = iter(
        [
            "2",  # select wechat (logged in → edit menu)
            "4",  # toggle enable/disable
            "",  # ESC wechat edit → back to channel select
            "2",  # select wechat again
            "5",  # remove channel
            "y",  # confirm
            "",  # ESC exit wizard
        ]
    )
    gw.run_wizard(str(p), input_fn=lambda _prompt: next(inputs2))
    assert gw.list_channels(str(p)) == []


def test_wechat_wizard_creates_default_config_and_runs_scan_before_options(
    tmp_path, monkeypatch, capsys
) -> None:
    p = tmp_path / "channels.yaml"
    login_calls: list[str] = []

    def _fake_login(name: str, *, state_dir: str | None = None) -> int:
        login_calls.append(name)
        return 0

    monkeypatch.setattr(gw, "wechat_login", _fake_login)
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "unconfigured")
    inputs = iter(
        [
            "2",  # select wechat (not logged in → add flow: scan + edit menu)
            "",  # ESC post-scan edit menu → back to channel select
            "",  # ESC exit wizard
        ]
    )
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    assert gw.run_wizard(str(p), input_fn=_input) == 0

    assert login_calls == ["wechat"]
    assert gw.list_channels(str(p)) == [{"name": "wechat", "type": "wechat", "enabled": True}]
    text = (capsys.readouterr().out + "\n".join(prompts)).lower()
    assert "account id" not in text
    assert "allowed users" not in text
    assert "base url" not in text
    assert "编辑字段" not in text


def test_wizard_remove_wechat_channel_deletes_owned_state(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "logged_in")
    (tmp_path / "wechat_context_tokens.json").write_text('{"acct:user": "ctx"}', encoding="utf-8")
    (tmp_path / "wechat").mkdir()
    (tmp_path / "wechat" / "wechat_auth.json").write_text("owned", encoding="utf-8")
    inputs = iter(
        [
            "2",  # select wechat (logged in → edit menu)
            "5",  # remove channel
            "y",  # confirm
            "",  # ESC exit wizard
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _prompt: next(inputs)) == 0

    assert gw.list_channels(str(p)) == []
    assert not (tmp_path / "wechat_context_tokens.json").exists()
    assert not (tmp_path / "wechat" / "wechat_auth.json").exists()


def test_existing_wechat_wizard_options_do_not_prompt_for_internal_fields(
    tmp_path, monkeypatch, capsys
) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "logged_in")
    inputs = iter(
        [
            "2",  # select existing wechat (logged in → edit menu)
            "2",  # status
            "",  # ESC wechat edit → back to channel select
            "",  # ESC exit wizard
        ]
    )
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    assert gw.run_wizard(str(p), input_fn=_input) == 0

    text = capsys.readouterr().out + "\n".join(prompts)
    assert "编辑字段" not in text
    assert "account id" not in text.lower()
    assert "allowed users" not in text.lower()
    assert "base url" not in text.lower()
    assert "logged_in" in text


# -- Feishu wizard -------------------------------------------------------


def _feishu_ws_channel(name: str = "feishu") -> ChannelConfig:
    return gw.build_channel_from_inputs(
        "feishu",
        name,
        {
            "connection_mode": "websocket",
            "app_id": "cli_app",
            "app_secret": "orig_plaintext_secret_xyz",
            "domain": "feishu",
            "bot_open_id": "ou_bot",
            "enabled": "true",
        },
    )


def test_wizard_add_feishu_interrupts_when_lark_oapi_missing(tmp_path, monkeypatch, capsys) -> None:
    p = tmp_path / "channels.yaml"
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: False)
    inputs = iter(["1", ""])  # select feishu (not logged in → add → dep_check fails), then exit

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    captured = capsys.readouterr()
    assert "lark-oapi not installed" in captured.err
    assert "pip install lark-oapi" in captured.err
    assert gw.list_channels(str(p)) == []


def test_wizard_add_feishu_websocket_defaults_to_scan_and_skips_webhook_fields(
    tmp_path, monkeypatch, capsys
) -> None:
    p = tmp_path / "channels.yaml"
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    monkeypatch.setattr(
        gw,
        "_feishu_scan_login",
        lambda input_fn: {
            "connection_mode": "websocket",
            "app_id": "cli_app",
            "app_secret": "secret",
            "domain": "feishu",
            "allowed_user_open_id": "ou_scanner",
        },
    )
    inputs = iter(["1", "1", ""])  # select feishu (add), websocket mode (idx 0), ESC exit
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    assert gw.run_wizard(str(p), input_fn=_input) == 0

    listed = gw.list_channels(str(p))
    assert listed == [{"name": "feishu", "type": "feishu", "enabled": True}]
    from orchestratord.im_gateway.config import load_config

    channel = load_config(str(p)).get_channel("feishu")
    assert channel.extra["connection_mode"] == "websocket"
    assert channel.extra["allowed_user_open_id"] == "ou_scanner"
    assert channel.webhook_url == ""
    assert "bot_open_id" not in channel.extra
    assert "websocket" not in channel.extra

    text = (capsys.readouterr().out + "\n".join(prompts)).lower()
    assert "webhook url" not in text
    assert "allowed user" not in text
    assert "ws reconnect" not in text
    assert "渠道名称" not in text
    assert "gateway 将自动重启" in text
    assert text.count("gateway 将自动重启") == 1


def test_wizard_add_feishu_webhook_mode_prompts_webhook_url(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    inputs = iter(
        [
            "1",  # select feishu (not logged in → add)
            "2",  # webhook mode (idx 1)
            "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef",  # webhook_url
            "sign_secret",  # secret
            "",  # ESC exit wizard
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    from orchestratord.im_gateway.config import load_config

    channel = load_config(str(p)).get_channel("feishu")
    assert channel.extra["connection_mode"] == "webhook"
    assert channel.webhook_url == "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef"
    assert channel.extra["secret"] == "sign_secret"


def test_wizard_edit_feishu_toggle_enabled(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _feishu_ws_channel())
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    inputs = iter(
        [
            "1",  # select feishu (logged in → edit menu)
            "2",  # toggle enable/disable (idx 1)
            "",  # ESC feishu edit → back to channel select
            "",  # ESC exit wizard
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    listed = gw.list_channels(str(p))
    assert listed[0]["enabled"] is False


def test_wizard_add_feishu_websocket_keeps_wechat_enabled(tmp_path, monkeypatch) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    monkeypatch.setattr(
        gw,
        "_feishu_scan_login",
        lambda input_fn: {
            "connection_mode": "websocket",
            "app_id": "cli_app",
            "app_secret": "secret",
            "domain": "feishu",
        },
    )
    inputs = iter(["1", "1", ""])  # select feishu (add), websocket mode (idx 0), ESC exit

    assert gw.run_wizard(str(p), input_fn=lambda _prompt: next(inputs)) == 0

    from orchestratord.im_gateway.config import load_config

    cfg = load_config(str(p))
    assert cfg.get_channel("wechat").enabled is True
    assert cfg.get_channel("feishu").enabled is True


def test_feishu_scan_login_uses_real_qr_registration_without_manual_prompts(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        gw,
        "_feishu_qr_register",
        lambda: {
            "app_id": "cli_app",
            "app_secret": "secret",
            "domain": "feishu",
            "open_id": "ou_scanner",
        },
    )

    def _unexpected_input(_prompt: str) -> str:
        raise AssertionError("successful scan login must not ask for manual credentials")

    result = gw._feishu_scan_login(_unexpected_input)

    captured = capsys.readouterr().out
    assert "占位" not in captured
    assert "第一条消息" not in captured
    assert result == {
        "connection_mode": "websocket",
        "app_id": "cli_app",
        "app_secret": "secret",
        "domain": "feishu",
        "allowed_user_open_id": "ou_scanner",
    }


def test_feishu_scan_login_falls_back_to_manual_when_scan_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(gw, "_feishu_qr_register", lambda: None)
    inputs = iter(["cli_app", "secret", "", "", "lark"])

    result = gw._feishu_scan_login(lambda _p: next(inputs))

    captured = capsys.readouterr().out
    assert "占位" not in captured
    assert "手动填写" in captured
    assert result == {
        "connection_mode": "websocket",
        "app_id": "cli_app",
        "app_secret": "secret",
        "domain": "lark",
    }


def test_wizard_edit_feishu_login_manual_masks_secret_and_keeps_values(
    tmp_path, monkeypatch, capsys
) -> None:
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _feishu_ws_channel())
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    # 新流程：ui.prompt 在 input_fn 路径下空行=ESC=中断；为"保留"字段需显式重输原值。
    inputs = iter(
        [
            "1",  # select feishu (logged in → edit menu)
            "1",  # reset (idx 0)
            "2",  # manual (idx 1)
            "cli_app",  # app_id: re-enter existing to "keep"
            "new_secret",  # app_secret: new value
            "enc_key",  # encrypt_key
            "ver_tok",  # verification_token
            "feishu",  # domain: re-enter existing to "keep"
            "ou_bot",  # bot_open_id: re-enter existing to "keep"
            "",  # ESC feishu edit → back to channel select
            "",  # ESC exit wizard
        ]
    )
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    assert gw.run_wizard(str(p), input_fn=_input) == 0

    from orchestratord.im_gateway.config import load_config

    channel = load_config(str(p)).get_channel("feishu")
    assert channel.extra["app_id"] == "cli_app"
    assert channel.extra["app_secret"] == "new_secret"
    assert channel.extra["domain"] == "feishu"
    assert channel.extra["encrypt_key"] == "enc_key"
    assert channel.extra["verification_token"] == "ver_tok"

    text = capsys.readouterr().out + "\n".join(prompts)
    assert "已配置" in text
    # never echo the existing plaintext secret value
    assert "orig_plaintext_secret_xyz" not in text


def test_wizard_edit_feishu_scan_login_updates_allowed_user(tmp_path, monkeypatch, capsys) -> None:
    p = tmp_path / "channels.yaml"
    existing = _feishu_ws_channel()
    existing.extra["allowed_user_open_id"] = "ou_old_scanner"
    gw.add_channel(str(p), existing)
    sender_state = tmp_path / "feishu_last_senders.json"
    sender_state.write_text('{"feishu":"oc_old_chat"}', encoding="utf-8")
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    monkeypatch.setattr(
        gw,
        "_feishu_scan_login",
        lambda input_fn: {
            "connection_mode": "websocket",
            "app_id": "new_app",
            "app_secret": "new_secret",
            "domain": "lark",
            "allowed_user_open_id": "ou_new_scanner",
        },
    )
    inputs = iter(
        [
            "1",  # select feishu (logged in → edit menu)
            "1",  # reset (idx 0)
            "1",  # scan (idx 0)
            "",  # ESC feishu edit → back to channel select
            "",  # ESC exit wizard
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _prompt: next(inputs)) == 0

    from orchestratord.im_gateway.config import load_config

    channel = load_config(str(p)).get_channel("feishu")
    assert channel.extra["app_id"] == "new_app"
    assert channel.extra["app_secret"] == "new_secret"
    assert channel.extra["domain"] == "lark"
    assert channel.extra["allowed_user_open_id"] == "ou_new_scanner"
    assert "bot_open_id" not in channel.extra
    assert not sender_state.exists()
    output = capsys.readouterr().out
    assert output.count("Gateway 将自动重启") == 1


# -- arrow-key wizard flow (input_fn fallback) ---------------------------


def test_run_wizard_esc_at_channel_select_exits(tmp_path) -> None:
    """频道选择层 ESC（空行）直接退出 setup。"""
    p = tmp_path / "channels.yaml"
    inputs = iter([""])  # ESC at channel select
    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0


def test_run_wizard_feishu_not_logged_in_runs_add_flow(tmp_path, monkeypatch) -> None:
    """无 feishu 渠道 → 选 feishu → 走新增流程（_wizard_add_feishu）。"""
    p = tmp_path / "channels.yaml"
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    monkeypatch.setattr(
        gw,
        "_feishu_scan_login",
        lambda input_fn: {
            "connection_mode": "websocket",
            "app_id": "cli_app",
            "app_secret": "secret",
            "domain": "feishu",
            "allowed_user_open_id": "ou_scanner",
        },
    )
    inputs = iter(
        [
            "1",  # select feishu (not configured -> add flow)
            "1",  # websocket mode (default)
            "",  # ESC to exit setup after add
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    listed = gw.list_channels(str(p))
    assert listed == [{"name": "feishu", "type": "feishu", "enabled": True}]


def test_run_wizard_wechat_not_logged_in_runs_add_flow(tmp_path, monkeypatch) -> None:
    """无 wechat 渠道 → 选 wechat → 走新增流程（扫码）。"""
    p = tmp_path / "channels.yaml"
    login_calls: list[str] = []

    def _fake_login(name: str, *, state_dir: str | None = None) -> int:
        login_calls.append(name)
        return 0

    monkeypatch.setattr(gw, "wechat_login", _fake_login)
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "unconfigured")
    inputs = iter(
        [
            "2",  # select wechat (not configured -> add flow)
            "",  # ESC to exit edit menu after scan
            "",  # ESC to exit setup
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    assert login_calls == ["wechat"]
    assert gw.list_channels(str(p)) == [{"name": "wechat", "type": "wechat", "enabled": True}]


def test_run_wizard_feishu_logged_in_goes_to_edit_menu(tmp_path, monkeypatch, capsys) -> None:
    """feishu 已登录 → 选 feishu → 进入编辑菜单（含"重置"而非"登录"）。"""
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _feishu_ws_channel())  # 已配置且 app_id/app_secret 齐全
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    inputs = iter(
        [
            "1",  # select feishu (logged in -> edit menu)
            "",  # ESC back to channel select
            "",  # ESC exit setup
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    text = capsys.readouterr().out
    # 新菜单用"重置"而非"登录"
    assert "重置" in text
    # 负向断言只针对 feishu 编辑菜单片段（到下一次回频道选择标题之前），
    # 避免被频道选择层的 wechat [未登录] 或 ESC 后再次渲染的菜单误伤
    feishu_edit_section = ""
    if "编辑 feishu [feishu]" in text:
        after = text.split("编辑 feishu [feishu]", 1)[-1]
        # 截到回频道选择标题之前
        feishu_edit_section = after.split("orchestratord 消息渠道配置", 1)[0]
    assert "登录" not in feishu_edit_section


def test_run_wizard_wechat_logged_in_goes_to_edit_menu(tmp_path, monkeypatch, capsys) -> None:
    """wechat 已登录 → 选 wechat → 进入编辑菜单（含"重置"）。"""
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), gw.build_default_channel("wechat"))
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "logged_in")
    inputs = iter(
        [
            "2",  # select wechat (logged in -> edit menu)
            "",  # ESC back to channel select
            "",  # ESC exit setup
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    text = capsys.readouterr().out
    assert "重置" in text


def test_run_wizard_esc_at_feishu_edit_returns_to_channel_select(tmp_path, monkeypatch) -> None:
    """feishu 编辑菜单 ESC → 回频道选择；再 ESC → 退出 setup。"""
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _feishu_ws_channel())
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    inputs = iter(
        [
            "1",  # select feishu -> edit menu
            "",  # ESC at feishu edit -> back to channel select
            "",  # ESC at channel select -> exit
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0


def test_feishu_edit_remove_then_reenter_runs_add_flow(tmp_path, monkeypatch) -> None:
    """feishu 已登录 → 编辑菜单选"移除" → 回频道选择 → 再选 feishu → 走新增流程。"""
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _feishu_ws_channel())
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    add_calls: list[int] = []
    orig_add = gw._wizard_add_feishu

    def _spy_add(cfg, path, ui):
        add_calls.append(1)
        return orig_add(cfg, path, ui)

    monkeypatch.setattr(gw, "_wizard_add_feishu", _spy_add)
    monkeypatch.setattr(
        gw,
        "_feishu_scan_login",
        lambda input_fn: {
            "connection_mode": "websocket",
            "app_id": "new_app",
            "app_secret": "new_secret",
            "domain": "feishu",
            "allowed_user_open_id": "ou_new_scanner",
        },
    )
    inputs = iter(
        [
            "1",  # select feishu (logged in -> edit menu)
            "3",  # remove channel
            "y",  # confirm remove
            "1",  # select feishu again (now removed -> add flow)
            "1",  # websocket mode
            "",  # ESC exit setup
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    assert add_calls == [1]  # 走了一次新增流程
    listed = gw.list_channels(str(p))
    assert listed == [{"name": "feishu", "type": "feishu", "enabled": True}]


def test_existing_slack_channel_remains_editable_but_not_addable(tmp_path, monkeypatch) -> None:
    """预置 slack 渠道 → 菜单含 slack 存量项 → 可编辑/移除，无"新增 slack"选项。"""
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("slack-ops", enabled=True))
    inputs = iter(
        [
            "3",  # select slack (存量项，第3项)
            "3",  # remove
            "y",  # confirm
            "",  # ESC exit setup
        ]
    )
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    assert gw.run_wizard(str(p), input_fn=_input) == 0

    assert gw.list_channels(str(p)) == []
    # 菜单提示中不应出现"新增 slack"
    text = "\n".join(prompts)
    assert "新增" not in text or "slack" not in text.lower().split("新增")[-1]


def test_feishu_manual_login_esc_aborts_without_saving(tmp_path, monkeypatch) -> None:
    """feishu 重置→手动填写中途 ESC → 返回主菜单，渠道未修改。"""
    p = tmp_path / "channels.yaml"
    original = _feishu_ws_channel()
    gw.add_channel(str(p), original)
    monkeypatch.setattr(gw, "_feishu_dependencies_available", lambda: True)
    inputs = iter(
        [
            "1",  # select feishu -> edit menu
            "1",  # reset
            "2",  # manual
            "",  # app_id: ESC -> abort manual
            "",  # ESC feishu edit -> back to channel select
            "",  # ESC exit setup
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    from orchestratord.im_gateway.config import load_config

    channel = load_config(str(p)).get_channel("feishu")
    # 渠道未变
    assert channel.extra["app_id"] == "cli_app"
    assert channel.extra["app_secret"] == "orig_plaintext_secret_xyz"


def test_edit_fields_esc_aborts_without_saving(tmp_path, monkeypatch) -> None:
    """slack 字段填写中途 ESC → 渠道未变。"""
    p = tmp_path / "channels.yaml"
    gw.add_channel(str(p), _slack("slack-ops", enabled=True))
    inputs = iter(
        [
            "3",  # select slack
            "1",  # edit fields
            "",  # webhook_url: ESC -> abort
            "",  # ESC slack edit -> back to channel select
            "",  # ESC exit
        ]
    )

    assert gw.run_wizard(str(p), input_fn=lambda _p: next(inputs)) == 0

    from orchestratord.im_gateway.config import load_config

    channel = load_config(str(p)).get_channel("slack-ops")
    assert channel.webhook_url == "https://hooks.example.com/services/T/B/abcdef0123456789"


def test_feishu_is_logged_in_detects_configured_credentials() -> None:
    """_feishu_is_logged_in: app_id+app_secret 齐全 → True。"""
    channel = _feishu_ws_channel()
    assert gw._feishu_is_logged_in(channel) is True

    bare = gw.build_channel_from_inputs("feishu", "feishu", {"connection_mode": "websocket"})
    assert gw._feishu_is_logged_in(bare) is False


def test_wechat_is_logged_in_reflects_login_status(monkeypatch) -> None:
    """_wechat_is_logged_in: 复用 wechat_login_status。"""
    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "logged_in (x)")
    assert gw._wechat_is_logged_in("wechat") is True

    monkeypatch.setattr(gw, "wechat_login_status", lambda name, state_dir=None: "unconfigured")
    assert gw._wechat_is_logged_in("wechat") is False
