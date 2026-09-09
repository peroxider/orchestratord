"""Tests for ``orchestratord web`` + ``serve --with-web`` (§10.1).

``web`` launches the Next.js client from ``apps/web``; ``serve
--with-web`` spawns the same launcher alongside uvicorn and must
terminate it on exit. Subprocess and uvicorn seams are faked — no
sockets, no pnpm, no Next.js needed.

Env hygiene: ``serve.run`` does ``os.environ.setdefault`` for the
daemon flags; the serve tests swap ``os.environ`` for a copy so the
leak never reaches later tests in the same pytest process.
"""

from __future__ import annotations

import argparse
import os
import sys
import types

import pytest

from orchestratord.cli.serve import add_serve_parser
from orchestratord.cli.serve import run as serve_run
from orchestratord.cli.web import add_web_parser, resolve_web_dir
from orchestratord.cli.web import run as web_run


def _web_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top", required=True)
    add_web_parser(subparsers)
    return parser.parse_args(["web", *argv])


def _serve_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top", required=True)
    add_serve_parser(subparsers)
    return parser.parse_args(["serve", *argv])


@pytest.fixture
def fake_web_dir(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("ORCHESTRATORD_WEB_DIR", raising=False)
    return web


@pytest.fixture
def isolated_env(monkeypatch):
    """serve.run mutates os.environ via setdefault — keep it hermetic."""
    monkeypatch.setattr(os, "environ", os.environ.copy())


class TestParser:
    def test_defaults(self) -> None:
        args = _web_args([])
        assert args.port == 3100
        assert args.dev is False
        assert args.host is None
        assert args.web_dir is None

    def test_overrides(self) -> None:
        args = _web_args(
            ["--port", "3200", "--dev", "--host", "127.0.0.1", "--web-dir", "/x"]
        )
        assert args.port == 3200
        assert args.dev is True
        assert args.host == "127.0.0.1"
        assert args.web_dir == "/x"


class TestResolveWebDir:
    def test_env_override(self, tmp_path, monkeypatch) -> None:
        fake = tmp_path / "envweb"
        fake.mkdir()
        (fake / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("ORCHESTRATORD_WEB_DIR", str(fake))
        assert resolve_web_dir() == fake

    def test_explicit_beats_env(self, fake_web_dir, tmp_path, monkeypatch) -> None:
        env_dir = tmp_path / "envweb"
        env_dir.mkdir()
        (env_dir / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("ORCHESTRATORD_WEB_DIR", str(env_dir))
        assert resolve_web_dir(str(fake_web_dir)) == fake_web_dir

    def test_missing_raises(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("ORCHESTRATORD_WEB_DIR", raising=False)
        monkeypatch.setattr(
            "orchestratord.cli.web._default_web_dir",
            lambda: tmp_path / "not-there",
        )
        with pytest.raises(SystemExit):
            resolve_web_dir(str(tmp_path / "also-not-there"))


class TestWebRun:
    @staticmethod
    def _capture_spawn(monkeypatch) -> dict:
        captured: dict = {}
        child = types.SimpleNamespace(wait=lambda: 0)

        def fake_spawn(cmd, cwd):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return child

        monkeypatch.setattr("orchestratord.cli.web._spawn", fake_spawn)
        return captured

    def test_prod_runs_next_start(self, fake_web_dir, monkeypatch) -> None:
        captured = self._capture_spawn(monkeypatch)
        args = _web_args(["--web-dir", str(fake_web_dir), "--port", "3200"])
        assert web_run(args) == 0
        assert captured["cmd"] == [
            "pnpm", "exec", "next", "start", "--port", "3200",
        ]
        assert captured["cwd"] == fake_web_dir

    def test_dev_mode_and_hostname(self, fake_web_dir, monkeypatch) -> None:
        captured = self._capture_spawn(monkeypatch)
        args = _web_args(
            ["--web-dir", str(fake_web_dir), "--dev", "--host", "0.0.0.0"]
        )
        assert web_run(args) == 0
        assert captured["cmd"] == [
            "pnpm", "exec", "next", "dev", "--port", "3100", "--hostname", "0.0.0.0",
        ]

    def test_missing_dir_exits_before_spawn(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv("ORCHESTRATORD_WEB_DIR", raising=False)
        monkeypatch.setattr(
            "orchestratord.cli.web._default_web_dir",
            lambda: tmp_path / "not-there",
        )

        def boom(*a, **kw):
            raise AssertionError("must not spawn when the directory is missing")

        monkeypatch.setattr("orchestratord.cli.web._spawn", boom)
        args = _web_args(["--web-dir", str(tmp_path / "also-not-there")])
        with pytest.raises(SystemExit):
            web_run(args)


class TestServeWithWeb:
    def test_with_web_spawns_before_uvicorn_and_terminates(
        self, isolated_env, monkeypatch
    ) -> None:
        spawned: dict = {}
        child = types.SimpleNamespace(
            terminate=lambda: spawned.setdefault("terminated", True)
        )

        def fake_launch(**kwargs):
            spawned.update(kwargs)
            return child

        monkeypatch.setattr("orchestratord.cli.web.launch_web_process", fake_launch)

        # serve.run() builds a uvicorn.Config and drives a Server subclass
        # (the D24 GOODBYE-drain override); the fake needs both.
        class FakeServer:
            def __init__(self, config) -> None:
                self.config = config

            def run(self) -> None:
                # The web child must already be up when the API starts.
                assert spawned, "launch_web_process must run before uvicorn"

        monkeypatch.setitem(
            sys.modules,
            "uvicorn",
            types.SimpleNamespace(
                Config=lambda app, **kw: types.SimpleNamespace(app=app, **kw),
                Server=FakeServer,
            ),
        )

        args = _serve_args(["--no-seed", "--with-web", "--web-port", "3210", "--web-dev"])
        assert serve_run(args) == 0
        assert spawned["port"] == 3210
        assert spawned["dev"] is True
        assert spawned.get("terminated") is True

    def test_without_flag_does_not_spawn(self, isolated_env, monkeypatch) -> None:
        def boom(**kwargs):
            raise AssertionError("launch_web_process must not be called")

        monkeypatch.setattr("orchestratord.cli.web.launch_web_process", boom)

        class FakeServer:
            def __init__(self, config) -> None:
                self.config = config

            def run(self) -> None:
                pass

        monkeypatch.setitem(
            sys.modules,
            "uvicorn",
            types.SimpleNamespace(
                Config=lambda app, **kw: None,
                Server=FakeServer,
            ),
        )
        assert serve_run(_serve_args(["--no-seed"])) == 0
