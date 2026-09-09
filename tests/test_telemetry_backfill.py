"""Telemetry backfill tests — local_days + report_backfill sweep rules.

Same environment strategy as ``test_telemetry_aggregator.py``: telemetry
writes are redirected into a pytest tmp dir by pointing
``ORCHESTRATORD_HOME`` at it and reloading the storage module (its base
dir is resolved at import time; ``reload`` mutates the module in place,
so previously-imported siblings pick up the new base dir through their
shared module ``__dict__``).
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def telemetry_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(tmp_path / "home"))
    from orchestratord.telemetry import storage

    importlib.reload(storage)
    yield storage
    importlib.reload(storage)


def _write_day(storage, day: str) -> None:
    path = storage.events_dir() / f"{day}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "command_run", "payload": {}}) + "\n")


def _day_from_today(offset_days: int) -> str:
    base = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    return time.strftime("%Y-%m-%d", time.localtime(base - 86400.0 * offset_days))


class _FakeApi:
    """In-memory stand-in for the GitCode issues API used by issue.py."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.patched: list[tuple[int, str]] = []
        self._next_number = 101

    def __call__(self, url: str, *, api_key: str, method: str = "GET", body: dict | None = None) -> Any:
        if method == "GET":
            # Existing open issues mirror what we created, so find-or-create
            # reuses the same issue number on refresh sweeps.
            return [
                {"title": title, "number": 101 + i}
                for i, title in enumerate(self.created)
            ]
        if method == "POST":
            number = self._next_number
            self._next_number += 1
            self.created.append(body["title"])
            return {"number": number}
        if method == "PATCH":
            issue_number = int(url.rsplit("/", 1)[-1])
            self.patched.append((issue_number, body["body"]))
            return {"ok": True}
        return None


@pytest.fixture
def fake_api(monkeypatch) -> _FakeApi:
    from orchestratord.telemetry.reporters import issue

    api = _FakeApi()
    monkeypatch.setattr(issue, "_api", api)
    return api


def _report_backfill(**kw: Any):
    from orchestratord.telemetry.reporters import report_backfill

    return report_backfill(owner="o", repo="r", api_key="k", title="Tel", **kw)


# ── storage.local_days ────────────────────────────────────────────────


def test_local_days_lists_only_date_files(telemetry_home) -> None:
    _write_day(telemetry_home, "2026-09-01")
    _write_day(telemetry_home, "2026-09-03")
    (telemetry_home.events_dir() / "notes.jsonl").write_text("{}\n", encoding="utf-8")

    assert telemetry_home.local_days() == ["2026-09-01", "2026-09-03"]


# ── report_backfill coverage ──────────────────────────────────────────


def test_backfill_all_reports_every_local_day(telemetry_home, fake_api) -> None:
    today = _day_from_today(0)
    for day in ("2026-09-01", "2026-09-02", today):
        _write_day(telemetry_home, day)

    results = _report_backfill(days="all")

    assert [day for day, ok, _ in results] == ["2026-09-01", "2026-09-02", today]
    assert all(ok for _, ok, _ in results)
    assert fake_api.created == ["Tel 2026-09-01", "Tel 2026-09-02", f"Tel {today}"]


def test_backfill_window_limits_to_recent_days(telemetry_home, fake_api) -> None:
    today = _day_from_today(0)
    for offset in (4, 2, 1, 0):
        _write_day(telemetry_home, _day_from_today(offset))

    results = _report_backfill(days=3)

    covered = [day for day, _, _ in results]
    assert covered == [_day_from_today(2), _day_from_today(1), today]
    assert _day_from_today(4) not in covered


# ── cursor rules: skip complete, refresh stale ────────────────────────


def test_backfill_refreshes_mid_day_upload_and_skips_complete_day(
    telemetry_home, fake_api
) -> None:
    from orchestratord.telemetry.reporters import issue

    stale_day = _day_from_today(2)
    complete_day = _day_from_today(1)
    today = _day_from_today(0)
    for day in (stale_day, complete_day):
        _write_day(telemetry_home, day)

    # stale_day was uploaded at 01:00 that day (before its data was
    # complete); complete_day was uploaded the morning after (complete).
    def _cursor_entry(day: str, hour_offset: float) -> dict[str, Any]:
        reported_at = time.mktime(time.strptime(day, "%Y-%m-%d")) + hour_offset
        return {"day": day, "reported_at": reported_at}

    issue._cursor_path().parent.mkdir(parents=True, exist_ok=True)
    issue._cursor_path().write_text(
        json.dumps(
            {
                f"o/r:Tel {stale_day}": _cursor_entry(stale_day, 3600.0),
                f"o/r:Tel {complete_day}": _cursor_entry(complete_day, 86400.0 + 7200.0),
            }
        ),
        encoding="utf-8",
    )

    results = _report_backfill(days="all")

    covered = [day for day, _, _ in results]
    assert covered == [stale_day, today]  # complete_day skipped
    # The stale day was refreshed onto its existing issue (#101).
    assert (101, fake_api.patched[0][1]) in fake_api.patched


def test_backfill_second_sweep_skips_now_complete_days(
    telemetry_home, fake_api
) -> None:
    stale_day = _day_from_today(1)
    _write_day(telemetry_home, stale_day)

    first = _report_backfill(days="all")
    assert [day for day, _, _ in first] == [stale_day, _day_from_today(0)]

    second = _report_backfill(days="all")
    # Only today remains — the past day is complete after its refresh.
    assert [day for day, _, _ in second] == [_day_from_today(0)]


def test_legacy_string_cursor_entry_is_refreshed_once(
    telemetry_home, fake_api
) -> None:
    from orchestratord.telemetry.reporters import issue

    day = _day_from_today(1)
    _write_day(telemetry_home, day)
    issue._cursor_path().parent.mkdir(parents=True, exist_ok=True)
    issue._cursor_path().write_text(
        json.dumps({f"o/r:Tel {day}": day}),  # pre-timestamp format
        encoding="utf-8",
    )

    results = _report_backfill(days="all")

    assert [d for d, _, _ in results] == [day, _day_from_today(0)]
    # ...and the upgraded cursor now reports the skip.
    assert [d for d, _, _ in _report_backfill(days="all")] == [_day_from_today(0)]


# ── config parsing ────────────────────────────────────────────────────


def test_parse_backfill_days_accepts_int_and_all() -> None:
    from orchestratord.config.schema import _parse_backfill_days

    assert _parse_backfill_days(1) == 1
    assert _parse_backfill_days(30) == 30
    assert _parse_backfill_days("7") == 7
    assert _parse_backfill_days("ALL") == "all"
    with pytest.raises(ValueError):
        _parse_backfill_days("week")
    with pytest.raises(ValueError):
        _parse_backfill_days(0)


# ── daemon wiring ─────────────────────────────────────────────────────


def test_report_telemetry_wires_backfill_days(telemetry_home, fake_api) -> None:
    from orchestratord.kernel.telemetry import report_telemetry

    for day in ("2026-09-01", "2026-09-02"):
        _write_day(telemetry_home, day)

    workflow = SimpleNamespace(
        telemetry=SimpleNamespace(
            reporting_enabled=True,
            api_key="k",
            report_owner="o",
            report_repo="r",
            issue_title="Tel",
            backfill_days="all",
        ),
        tracker=SimpleNamespace(api_key="", owner="", repo=""),
    )
    report_telemetry(workflow)

    assert fake_api.created == [
        "Tel 2026-09-01",
        "Tel 2026-09-02",
        f"Tel {_day_from_today(0)}",
    ]


def test_report_telemetry_noop_when_disabled(telemetry_home, fake_api) -> None:
    from orchestratord.kernel.telemetry import report_telemetry

    _write_day(telemetry_home, "2026-09-01")
    workflow = SimpleNamespace(
        telemetry=SimpleNamespace(reporting_enabled=False, api_key="k"),
        tracker=None,
    )
    report_telemetry(workflow)

    assert fake_api.created == []


# ── CLI ───────────────────────────────────────────────────────────────


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    from orchestratord.cli.telemetry import add_telemetry_parser

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="subcommand", required=True)
    add_telemetry_parser(subs)
    return parser.parse_args(["telemetry", *argv])


def test_cli_report_missing_target_errors(capsys) -> None:
    from orchestratord.cli.telemetry import run

    args = _parse_cli(["report", "--days", "all"])
    assert run(args) == 1
    assert "--owner" in capsys.readouterr().err


def test_cli_report_invalid_days_errors(capsys) -> None:
    from orchestratord.cli.telemetry import run

    args = _parse_cli(
        ["report", "--days", "week", "--owner", "o", "--repo", "r", "--api-key", "k"]
    )
    assert run(args) == 2
    assert "--days" in capsys.readouterr().err


def test_cli_report_prints_per_day_results(
    telemetry_home, monkeypatch, capsys
) -> None:
    from orchestratord.cli.telemetry import run

    _write_day(telemetry_home, "2026-09-01")
    seen: dict[str, Any] = {}

    def fake_backfill(**kw: Any):
        seen.update(kw)
        return [("2026-09-01", True, "issue #7 updated")]

    # _run_report imports report_backfill from the reporters package at
    # call time — patch it there.
    import orchestratord.telemetry.reporters as reporters_pkg

    monkeypatch.setattr(reporters_pkg, "report_backfill", fake_backfill)

    args = _parse_cli(
        ["report", "--days", "all", "--owner", "o", "--repo", "r", "--api-key", "k"]
    )
    assert run(args) == 0
    assert "✓ 2026-09-01: issue #7 updated" in capsys.readouterr().out
    assert seen["days"] == "all"
    assert seen["title"] == "Orchestratord Telemetry"
