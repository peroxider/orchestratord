"""``issue show`` usage display must fall back to the registry record
when no session snapshots exist — backends that report usage via
SESSION_COMPLETE payloads (dsh, opencode) persist it as
``run_token_usage`` on the record, not as claude-style snapshots.
"""

from __future__ import annotations

from types import SimpleNamespace

from orchestratord.cli.issue import _print_session_usage


def test_usage_falls_back_to_registry_record(capsys) -> None:
    record = SimpleNamespace(
        previous_run_ids=[],
        run_id="nonexistent-run-for-test",
        run_token_usage={
            "input": 281468,
            "output": 16458,
            "reasoning": 25535,
            "cache_read": 7841792,
        },
    )
    _print_session_usage(record)
    out = capsys.readouterr().out
    assert "Usage (last)" in out
    assert "input=281468" in out
    assert "output=16458" in out
    assert "cost not reported" in out


def test_usage_silent_when_nothing_reported(capsys) -> None:
    record = SimpleNamespace(
        previous_run_ids=[],
        run_id="nonexistent-run-for-test",
        run_token_usage={},
    )
    _print_session_usage(record)
    assert "Usage" not in capsys.readouterr().out
