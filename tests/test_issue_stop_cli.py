"""Regression tests for ``issue stop --yes`` non-interactive parameter.

Verifies that ``--yes/-y`` skips the confirmation prompt and dispatches
the stop command directly, while omitting ``--yes`` on a TTY still
prompts for confirmation.

Acceptance criteria (issue #18):
  1. ``--yes`` skips the "Stop agent for issue 7? [y/N]:" prompt.
  2. Without ``--yes`` the TTY prompt is still shown.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

from orchestratord.cli.issue import _run_stop, add_issue_parser

# ---------------------------------------------------------------------------
# Parser-level tests
# ---------------------------------------------------------------------------


def test_stop_parser_accepts_yes_flag() -> None:
    """``--yes`` and ``-y`` must be accepted by the stop parser."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_issue_parser(subparsers)

    args = parser.parse_args(["issue", "stop", "--id", "5", "--yes"])
    assert args.yes is True
    assert args.id == "5"

    args_short = parser.parse_args(["issue", "stop", "--id", "5", "-y"])
    assert args_short.yes is True
    assert args_short.id == "5"


def test_stop_parser_defaults_yes_false() -> None:
    """Without ``--yes``, the ``yes`` field must default to ``False``."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_issue_parser(subparsers)

    args = parser.parse_args(["issue", "stop", "--id", "5"])
    assert args.yes is False


# ---------------------------------------------------------------------------
# Runtime behavior tests
# ---------------------------------------------------------------------------


def _make_args(
    issue_id: str = "test-issue-1",
    yes: bool = False,
    no_wait: bool = True,
) -> SimpleNamespace:
    """Build a SimpleNamespace emulating parsed args for ``_run_stop``.

    ``no_wait=True`` by default to avoid socket connection attempts
    in unit tests (the socket path will not exist in an isolated temp
    directory).
    """
    return SimpleNamespace(
        id=issue_id,
        yes=yes,
        no_wait=no_wait,
        workspace=None,
        workflow=None,
    )


def test_stop_with_yes_skips_prompt(tmp_path: Path, capsys, monkeypatch) -> None:
    """``--yes`` must skip the confirmation prompt and go straight to
    the stop dispatch — ``input()`` must never be called.
    """
    # Fail the test if input() is ever invoked: --yes must be non-interactive.
    def _fail_on_input(*_args, **_kwargs):
        raise AssertionError("input() must not be called when --yes is set")

    monkeypatch.setattr("builtins.input", _fail_on_input)

    args = _make_args(yes=True)
    # No registry exists — the code will warn but with --yes it should
    # NOT prompt and proceed to the control-file fallback.
    rc = _run_stop(args, registry_path=None, workspace_root=str(tmp_path))

    # The stop command should be dispatched (either via control file or
    # socket). Since no socket exists, it falls back to control file.
    assert rc == 0, f"Expected exit code 0, got {rc}"

    out = capsys.readouterr().out
    # Must NOT contain the confirmation prompt text
    assert "Stop agent for issue" not in out, (
        "With --yes, the confirmation prompt must not appear"
    )
    assert "Stop cancelled" not in out, (
        "With --yes, stop must not be cancelled"
    )
    # Must contain the "stop" control command dispatch message
    assert "sending stop command" in out.lower(), (
        "With --yes, stop must be dispatched"
    )


def test_stop_without_yes_still_prompts(tmp_path: Path, capsys, monkeypatch) -> None:
    """Without ``--yes``, ``input()`` must be called with the
    confirmation prompt (simulated TTY behavior unchanged).
    """
    prompt_seen: list[str] = []

    def mock_input(prompt: str = "") -> str:
        prompt_seen.append(prompt)
        # Press Enter → default reject
        return ""

    monkeypatch.setattr("builtins.input", mock_input)

    args = _make_args(yes=False)
    rc = _run_stop(args, registry_path=None, workspace_root=str(tmp_path))

    assert rc == 0, f"Expected exit code 0 (cancelled), got {rc}"
    assert prompt_seen, "Without --yes, input() must be called"
    assert "Stop agent for issue" in prompt_seen[0] or "stop control file" in prompt_seen[0], (
        f"Expected a confirmation prompt, got: {prompt_seen}"
    )
    out = capsys.readouterr().out
    assert "Stop cancelled" in out, "Default Enter must cancel the stop"


def test_stop_without_yes_cancels_on_n(tmp_path: Path, capsys, monkeypatch) -> None:
    """Without ``--yes``, entering 'n' at the prompt must cancel."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    args = _make_args(yes=False)
    rc = _run_stop(args, registry_path=None, workspace_root=str(tmp_path))

    assert rc == 0, f"Expected exit code 0 (cancelled), got {rc}"
    out = capsys.readouterr().out
    assert "Stop cancelled" in out, "Entering 'n' must cancel the stop"


def test_stop_with_yes_writes_control_file(tmp_path: Path, capsys, monkeypatch) -> None:
    """``--yes`` must write the stop control file without any prompt."""
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )

    args = _make_args(yes=True)
    rc = _run_stop(args, registry_path=None, workspace_root=str(tmp_path))
    assert rc == 0, f"Expected exit code 0, got {rc}"

    control_dir = tmp_path / ".orchestrator_control"
    control_files = list(control_dir.glob("stop_*.control"))
    assert len(control_files) == 1, (
        f"Expected exactly one stop control file, got {len(control_files)}"
    )
    payload = control_files[0].read_text(encoding="utf-8")
    assert "stop" in payload
    assert args.id in payload


def test_stop_without_yes_does_not_write_control_file_on_cancel(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Without ``--yes`` and user cancels, no control file must be written."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    args = _make_args(yes=False)
    rc = _run_stop(args, registry_path=None, workspace_root=str(tmp_path))

    assert rc == 0, f"Expected exit code 0 (cancelled), got {rc}"
    control_dir = tmp_path / ".orchestrator_control"
    if control_dir.exists():
        control_files = list(control_dir.glob("stop_*.control"))
        assert len(control_files) == 0, (
            "No stop control file should be written when cancelled"
        )