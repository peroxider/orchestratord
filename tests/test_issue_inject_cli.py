"""``issue inject`` parameter parity and feedback.

Historical defects: hints never landed, the
command gave zero feedback on success, ``--workspace`` was not
supported (unlike list/show/tail/clarify), and running from certain
directories failed with "Could not find workspace".
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.cli.issue import _run_inject, add_issue_parser
from orchestratord.issue_registry import IssueRegistry


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace whose registry knows issue 5 with its own subdir."""
    ws = tmp_path / "ws"
    ws.mkdir()
    reg = IssueRegistry(ws / "issue_registry.json")
    reg.register(issue_id="5", issue_identifier="ISSUE-5", workspace_path=str(ws))
    (ws / "ISSUE-5").mkdir()
    monkeypatch.delenv("ORCHESTRATORD_WORKSPACE_ROOT", raising=False)
    # CWD is deliberately NOT the workspace — the historical failure mode.
    monkeypatch.chdir(tmp_path)
    return ws


def _args(workspace: Path | None) -> SimpleNamespace:
    return SimpleNamespace(
        id="5",
        hint="run pytest first",
        workspace=str(workspace) if workspace else None,
        list_hints=False,
        remove_hint=None,
        no_wait=True,
    )


def test_inject_resolves_workspace_from_cli_arg(workspace: Path, capsys) -> None:
    rc = _run_inject(_args(workspace))

    assert rc == 0
    hints_file = workspace / "ISSUE-5" / ".operator_hints.md"
    assert hints_file.exists(), "hint must be persisted in the issue workspace"
    assert "run pytest first" in hints_file.read_text()
    out = capsys.readouterr().out
    assert "hint injected" in out, "success must produce visible feedback"


def test_inject_without_workspace_uses_env_var_fallback(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --workspace → the documented ORCHESTRATORD_WORKSPACE_ROOT
    fallback must resolve (error message points users at it).
    """
    monkeypatch.setenv("ORCHESTRATORD_WORKSPACE_ROOT", str(workspace))
    rc = _run_inject(_args(workspace=None))
    assert rc == 0
    assert (workspace / "ISSUE-5" / ".operator_hints.md").exists()


def test_inject_parser_accepts_workspace_arg() -> None:
    """--workspace must be part of the inject parser (parity with
    list/show/tail/clarify).
    """
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_issue_parser(subparsers)

    args = parser.parse_args(
        ["issue", "inject", "--id", "5", "--workspace", "/tmp/somewhere", "hint text"]
    )
    assert args.workspace == "/tmp/somewhere"
    assert args.hint == "hint text"
    assert args.no_wait is False
