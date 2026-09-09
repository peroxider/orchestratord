"""Telemetry reporter — push the daily summary to a GitCode issue.

Find-or-create an issue whose title matches ``issue_title`` in the
configured repo, then update its body with the day's Markdown summary.
Each day gets its own issue (the title carries the date), so a past
day's report is never overwritten by a later date.

Cursor-based dedupe: the cursor (``report_cursor.json``, persisted next
to the events store) records *which* day was last pushed per
(owner, repo, title) and *when* — a day reported before it ended (e.g.
a mid-day upload) is refreshed with the complete file by
:func:`report_backfill` on a later sweep, then never touched again.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..storage import local_days, telemetry_dir

_CURSOR_FILENAME = "report_cursor.json"


def _cursor_path() -> Path:
    return telemetry_dir() / _CURSOR_FILENAME


def _read_cursor() -> dict[str, Any]:
    p = _cursor_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_cursor(key: str, day: str) -> None:
    cur = _read_cursor()
    cur[key] = {"day": day, "reported_at": time.time()}
    telemetry_dir().mkdir(parents=True, exist_ok=True)
    _cursor_path().write_text(
        json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _entry_day(value: Any) -> str:
    """Day part of a cursor value (timestamped dict or legacy plain string)."""
    if isinstance(value, dict):
        return str(value.get("day") or "")
    return str(value or "")


def _entry_reported_at(value: Any) -> float:
    """Report time of a cursor value; legacy string entries carry none."""
    if isinstance(value, dict):
        try:
            return float(value.get("reported_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _full_title(title: str, day: str) -> str:
    # 约定：每个用户每天一个 issue —— 标题带日期（每天一个独立 issue，
    # 旧天的数据保留在当天的 issue 里，不被后续日期覆盖）。
    return f"{title} {day}" if day not in title else title


def _day_end_ts(day: str) -> float:
    """Local timestamp of the midnight right after ``day``.

    Uploads before this point saw an incomplete day — the day's events
    keep landing until midnight, so a summary pushed earlier is stale
    and worth refreshing once.
    """
    try:
        return time.mktime(time.strptime(day, "%Y-%m-%d")) + 86400.0
    except ValueError:
        return float("inf")


def _api(url: str, *, api_key: str, method: str = "GET", body: dict | None = None) -> dict | None:
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_msg": exc.read().decode()[:200]}
    except Exception:
        return None


def _find_or_create_issue(*, owner: str, repo: str, api_key: str, title: str) -> int | None:
    # search existing open issues with the exact title
    issues = _api(
        f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues?state=open&per_page=100",
        api_key=api_key,
    )
    items = issues if isinstance(issues, list) else (issues or {}).get("data", [])
    for it in items:
        if str(it.get("title", "")).strip() == title:
            # GitCode's issues API paths use the public number, not the
            # internal id — prefer number so follow-up calls (PATCH etc.)
            # target the right issue.
            return it.get("number") or it.get("id")
    created = _api(
        f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues",
        api_key=api_key,
        method="POST",
        body={"title": title, "body": ""},
    )
    if isinstance(created, dict) and created.get("_http_error"):
        return None
    return created.get("number") or created.get("id") if created else None


def _update_issue(*, owner: str, repo: str, api_key: str, issue_id: int, body: str) -> bool:
    result = _api(
        f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{issue_id}",
        api_key=api_key,
        method="PATCH",
        body={"body": body},
    )
    return not (isinstance(result, dict) and result.get("_http_error"))


def report_day(
    *,
    owner: str,
    repo: str,
    api_key: str,
    title: str,
    day: str | None = None,
    summary: dict[str, Any] | None = None,
    rendered: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Push one day's summary to the telemetry issue. Returns (ok, detail)."""
    from ..aggregator import aggregate_day, render_summary_markdown

    day = day or time.strftime("%Y-%m-%d")
    full_title = _full_title(title, day)
    key = f"{owner}/{repo}:{full_title}"
    cursor = _read_cursor()
    if not force and _entry_day(cursor.get(key)) == day:
        return False, f"{day} already reported (cursor)"

    summary = summary or aggregate_day(day)
    rendered = rendered or render_summary_markdown(summary)
    if not owner or not repo or not api_key:
        return False, "missing owner/repo/api_key"

    issue_id = _find_or_create_issue(
        owner=owner, repo=repo, api_key=api_key, title=full_title
    )
    if not issue_id:
        return False, "find/create issue failed"

    ok = _update_issue(owner=owner, repo=repo, api_key=api_key, issue_id=issue_id, body=rendered)
    if ok:
        _write_cursor(key, day)
    return ok, f"issue #{issue_id} updated" if ok else "update failed"


def _window_days(count: int) -> set[str]:
    """The last ``count`` local calendar dates, today included."""
    today = time.strftime("%Y-%m-%d")
    base = time.mktime(time.strptime(today, "%Y-%m-%d"))
    return {
        time.strftime("%Y-%m-%d", time.localtime(base - 86400.0 * offset))
        for offset in range(count)
    }


def report_backfill(
    *,
    owner: str,
    repo: str,
    api_key: str,
    title: str,
    days: int | str = 1,
) -> list[tuple[str, bool, str]]:
    """Push the covered days' summaries, backfilling what earlier sweeps missed.

    ``days`` selects coverage: ``"all"`` = every day with a local events
    file; an int N = the last N calendar days including today (days
    without a local file are naturally skipped). Per day:

    - today is always re-reported — its summary grows during the day;
    - a past day is reported only when its cursor entry is missing, in
      the legacy format, or was written before that day ended — i.e. a
      mid-day upload gets refreshed once with the complete file, and a
      fully reported day costs no network calls.

    Returns one ``(day, ok, detail)`` row per day actually pushed.
    """
    if not owner or not repo or not api_key:
        raise ValueError("report_backfill requires owner, repo and api_key")

    today = time.strftime("%Y-%m-%d")
    local = set(local_days())
    if days == "all":
        candidates = set(local)
    else:
        count = int(days)
        if count < 1:
            raise ValueError("days must be >= 1 or 'all'")
        candidates = _window_days(count) & local
    candidates.add(today)

    cursor = _read_cursor()
    results: list[tuple[str, bool, str]] = []
    for day in sorted(candidates):
        if day != today:
            key = f"{owner}/{repo}:{_full_title(title, day)}"
            entry = cursor.get(key)
            if (
                entry is not None
                and _entry_day(entry) == day
                and _entry_reported_at(entry) >= _day_end_ts(day)
            ):
                continue  # complete report already stored remotely
        ok, detail = report_day(
            owner=owner, repo=repo, api_key=api_key, title=title, day=day, force=True
        )
        results.append((day, ok, detail))
    return results


def cursor_for(owner: str, repo: str, title: str) -> str:
    return _entry_day(_read_cursor().get(f"{owner}/{repo}:{title}", ""))
