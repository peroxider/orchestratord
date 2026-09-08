"""Tests for the ``orchestratord serve`` CLI subcommand (Phase 0, §5.5.1).

The command is a thin uvicorn launcher over ``orchestratord.api.app``; these
tests cover the parser defaults and that ``run`` hands uvicorn the ASGI app
string plus the host/port/reload options without actually binding a socket.
"""

from __future__ import annotations

import argparse
import sys
import types

from orchestratord.cli.serve import add_serve_parser, run


def _make_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top", required=True)
    add_serve_parser(subparsers)
    return parser.parse_args(["serve", *argv])


class TestParser:
    def test_defaults(self) -> None:
        args = _make_args([])
        assert args.host == "0.0.0.0"
        assert args.port == 9000
        assert args.reload is False

    def test_overrides(self) -> None:
        args = _make_args(["--host", "127.0.0.1", "--port", "8080", "--reload"])
        assert args.host == "127.0.0.1"
        assert args.port == 8080
        assert args.reload is True


class TestRun:
    def test_run_launches_uvicorn(self, monkeypatch) -> None:
        calls: dict[str, object] = {}

        def fake_run(app: str, **kwargs: object) -> None:
            calls["app"] = app
            calls.update(kwargs)

        monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=fake_run))

        args = _make_args(["--host", "127.0.0.1", "--port", "9123", "--no-seed"])
        assert run(args) == 0
        assert calls["app"] == "orchestratord.api.app:app"
        assert calls["host"] == "127.0.0.1"
        assert calls["port"] == 9123
        assert calls["reload"] is False
