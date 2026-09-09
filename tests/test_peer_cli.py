"""``orchestratord peer`` CLI tests (AC10: exit codes + stdout).

The registry verbs that need Postgres are covered live in
``tests/db_integration/test_peer_registry.py``; this module pins the
pure-argparse surface and the D26 group verbs (membership-only
authorization, no owner) against the CLI's process-global
:class:`GroupManager`.
"""

from __future__ import annotations

import argparse

import pytest

from orchestratord.peer import card


@pytest.fixture(autouse=True)
def fresh_group_state(monkeypatch, tmp_path):
    """Fresh orch_id + empty group manager for every test."""
    monkeypatch.setattr(card, "ORCH_ID_PATH", tmp_path / "data" / "orch_id")
    monkeypatch.delenv("ORCHESTRATORD_INSTANCE_NAME", raising=False)
    from orchestratord.cli.peer import reset_group_manager

    reset_group_manager()
    yield
    reset_group_manager()


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="orchestratord")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    from orchestratord.cli.peer import add_peer_parser

    add_peer_parser(subparsers)
    return parser.parse_args(["peer", *argv])


def _first_group_id() -> str:
    from orchestratord.cli.peer import get_group_manager

    groups = get_group_manager().all_groups()
    assert len(groups) == 1
    return groups[0].group_id


def test_help_lists_all_seven_subcommands(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("--help")
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for verb in (
        "list", "invite", "accept", "reject", "remove", "leave", "group"
    ):
        assert verb in out


def test_group_list_empty_prints_placeholder(capsys) -> None:
    from orchestratord.cli.peer import run

    assert run(_parse("group", "list")) == 0
    assert "(no groups)" in capsys.readouterr().out


def test_group_create_prints_group_and_members(capsys) -> None:
    from orchestratord.cli.peer import run

    code = run(
        _parse(
            "group", "create", "--name", "duo",
            "--member", "orch-a", "--member", "orch-b",
        )
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("created grp-")
    assert "['orch-a', 'orch-b']" in out


def test_group_add_by_member_succeeds(capsys) -> None:
    from orchestratord.cli.peer import get_group_manager, run

    local = get_group_manager()._local_orch_id
    run(
        _parse(
            "group", "create", "--name", "duo",
            "--member", local, "--member", "orch-b",
        )
    )
    code = run(
        _parse("group", "add", "--group-id", _first_group_id(), "--member", "orch-c")
    )
    assert code == 0
    assert "orch-c" in capsys.readouterr().out


def test_group_add_by_non_member_is_denied_d26(capsys) -> None:
    """D26: authorization is "caller is a member" — an outsider cannot add."""
    from orchestratord.cli.peer import run

    run(
        _parse(
            "group", "create", "--name", "duo",
            "--member", "orch-a", "--member", "orch-b",
        )
    )
    code = run(
        _parse(
            "group", "add", "--group-id", _first_group_id(),
            "--member", "orch-c", "--caller", "orch-outsider",
        )
    )
    assert code == 1
    assert "group error" in capsys.readouterr().err


def test_group_remove_by_member_kicks_d26(capsys) -> None:
    """D26: any member can kick — there is no owner to consult."""
    from orchestratord.cli.peer import get_group_manager, run

    local = get_group_manager()._local_orch_id
    run(
        _parse(
            "group", "create", "--name", "duo",
            "--member", local, "--member", "orch-b",
        )
    )
    code = run(
        _parse("group", "remove", "--group-id", _first_group_id(), "--member", "orch-b")
    )
    assert code == 0
    assert f"['{local}']" in capsys.readouterr().out


def test_group_remove_missing_member_fails(capsys) -> None:
    from orchestratord.cli.peer import get_group_manager, run

    local = get_group_manager()._local_orch_id
    run(
        _parse(
            "group", "create", "--name", "duo",
            "--member", local, "--member", "orch-b",
        )
    )
    code = run(
        _parse("group", "remove", "--group-id", _first_group_id(), "--member", "orch-ghost")
    )
    assert code == 1


def test_leave_removes_local_membership(capsys) -> None:
    from orchestratord.cli.peer import get_group_manager, run

    local = get_group_manager()._local_orch_id
    assert (
        run(
            _parse(
                "group", "create", "--name", "duo",
                "--member", local, "--member", "orch-b",
            )
        )
        == 0
    )
    code = run(_parse("leave", "--group-id", _first_group_id()))
    assert code == 0
    # Only the local member leaves; the group keeps its remaining peers.
    assert "['orch-b']" in capsys.readouterr().out


def test_leave_unknown_group_fails(capsys) -> None:
    from orchestratord.cli.peer import run

    assert run(_parse("leave", "--group-id", "grp-does-not-exist")) == 1
    assert "group error" in capsys.readouterr().err


def test_unknown_group_subcommand_exits_2() -> None:
    from orchestratord.cli.peer import run

    ns = _parse("group", "list")
    ns.group_subcommand = "bogus"
    assert run(ns) == 2


def test_unknown_peer_subcommand_exits_2() -> None:
    from orchestratord.cli.peer import run

    assert run(argparse.Namespace(peer_subcommand="bogus")) == 2
