"""Tests for the ``orchestratord skills`` CLI subcommands (Scheme D).

Covers ``skills list`` / ``skills show`` / ``skills verify`` against the
real builtin skills shipped in ``orchestratord.skills.builtin``.
"""

from __future__ import annotations

import argparse

import pytest

from orchestratord.cli.skills import add_skills_parser, run


def _make_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top", required=True)
    add_skills_parser(subparsers)
    return parser.parse_args(["skills", *argv])


class TestList:
    def test_list_returns_zero_and_prints_skills(self, capsys):
        args = _make_args(["list"])
        assert run(args) == 0
        out = capsys.readouterr().out
        assert "mode-selector" in out
        assert "capability-explainer" in out
        assert "failure-recovery" in out
        assert "NAME" in out and "STALE" in out


class TestShow:
    def test_show_prints_skill_md(self, capsys):
        args = _make_args(["show", "mode-selector"])
        assert run(args) == 0
        out = capsys.readouterr().out
        assert "SKILL.md" in out
        assert "Mode Selector" in out

    def test_show_unknown_returns_one(self, capsys):
        args = _make_args(["show", "no-such-skill"])
        assert run(args) == 1
        err = capsys.readouterr().err
        assert "not found" in err


class TestVerify:
    def test_verify_fresh_skills_returns_zero(self, capsys):
        args = _make_args(["verify"])
        code = run(args)
        assert code == 0
        out = capsys.readouterr().out
        assert "verified fresh" in out


class TestUnknownSubcommand:
    def test_unknown_returns_two(self, capsys):
        args = _make_args(["list"])
        args.skills_subcommand = "bogus"
        assert run(args) == 2
        err = capsys.readouterr().err
        assert "Unknown skills subcommand" in err


@pytest.mark.parametrize("argv", [["list"], ["show", "mode-selector"], ["verify"]])
def test_subcommands_do_not_raise(argv):
    args = _make_args(argv)
    assert run(args) in (0, 1)
