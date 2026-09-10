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


class _ApiResult:
    """Outcome of one GitCode API call.

    Replaces the previous ``{"_http_error": code}`` sentinel dict so a
    failed request is a distinct type, not a payload shape. ``data``
    holds the parsed JSON body on success; ``error`` / ``http_code``
    describe the failure when ``ok`` is false.
    """

    __slots__ = ("data", "error", "http_code")

    def __init__(
        self,
        *,
        data: Any = None,
        error: str | None = None,
        http_code: int | None = None,
    ) -> None:
        self.data = data
        self.error = error
        self.http_code = http_code

    @property
    def ok(self) -> bool:
        return self.error is None

    def __bool__(self) -> bool:
        return self.ok


def _api(url: str, *, api_key: str, method: str = "GET", body: dict | None = None) -> _ApiResult:
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _ApiResult(data=json.load(resp))
    except urllib.error.HTTPError as exc:
        return _ApiResult(error=exc.read().decode()[:200], http_code=exc.code)
    except Exception:
        return _ApiResult(error="request failed")


class GitCodeIssueClient:
    """Small GitCode issues-API client bound to one ``(owner, repo)`` pair.

    Holds the connection credentials so the ``owner/repo/api_key``
    triplet is not threaded through every call, and models failures with
    :class:`_ApiResult` instead of sentinel dicts. The
    ``repos/{owner}/{repo}/issues`` URL prefix is constructed here, once,
    instead of being rebuilt at every call site.
    """

    _BASE_URL = "https://api.gitcode.com/api/v5/repos"

    def __init__(self, *, owner: str, repo: str, api_key: str) -> None:
        self.owner = owner
        self.repo = repo
        self.api_key = api_key

    def _request(
        self, path: str, *, method: str = "GET", body: dict | None = None
    ) -> _ApiResult:
        return _api(
            f"{self._BASE_URL}/{self.owner}/{self.repo}{path}",
            api_key=self.api_key,
            method=method,
            body=body,
        )

    def find_or_create(self, title: str) -> int | None:
        """Return the public number of the open issue with ``title``,
        creating it when missing. ``None`` when the API call failed.

        GitCode's issues API paths use the public number, not the
        internal id — prefer number so follow-up calls (PATCH etc.)
        target the right issue.
        """
        # Search existing open issues with the exact title first.
        result = self._request("/issues?state=open&per_page=100")
        if not result.ok:
            return None
        items = result.data if isinstance(result.data, list) else (result.data or {}).get("data", [])
        for it in items:
            if str(it.get("title", "")).strip() == title:
                return it.get("number") or it.get("id")
        created = self._request(
            "/issues", method="POST", body={"title": title, "body": ""}
        )
        if not created.ok:
            return None
        return created.data.get("number") or created.data.get("id") if created.data else None

    def update(self, issue_id: int, body: str) -> bool:
        """Replace an issue body. ``True`` on success."""
        result = self._request(
            f"/issues/{issue_id}", method="PATCH", body={"body": body}
        )
        return result.ok


def _trend_section(day: str, *, window: int = 7) -> str:
    """Render a compact last-N-days trend table from local event files.

    Days without a local events file are skipped; the reported day itself
    is the last row. Pure local aggregation — no network calls.
    """
    from ..aggregator import USD_TO_CNY_RATE, aggregate_day

    base = time.mktime(time.strptime(day, "%Y-%m-%d"))
    rows: list[str] = []
    for offset in range(window - 1, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(base - 86400.0 * offset))
        summary = aggregate_day(d)
        if not summary.get("events"):
            continue
        ended = summary.get("sessions_ended") or 0
        ok = summary.get("sessions_succeeded") or 0
        rate = (summary.get("unattended") or {}).get("rate")
        rate_text = f"{rate * 100:.0f}%" if rate is not None else "-"
        cost = (summary.get("total_cost_usd") or 0.0) * USD_TO_CNY_RATE
        rows.append(f"| {d} | {ended} | {ok}/{ended} | {rate_text} | ¥{cost:,.2f} |")
    if not rows:
        return ""
    return "\n".join(
        [
            "## 近 7 天趋势",
            "",
            "| 日期 | 会话结束 | 成功/结束 | 无人干预闭环率 | 成本 CNY |",
            "|------|----------|-----------|----------------|----------|",
            *rows,
        ]
    )


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
    if rendered is None:
        rendered = render_summary_markdown(summary)
        trend = _trend_section(day)
        if trend:
            rendered = f"{rendered}\n\n{trend}"
    if not owner or not repo or not api_key:
        return False, "missing owner/repo/api_key"

    client = GitCodeIssueClient(owner=owner, repo=repo, api_key=api_key)
    issue_id = client.find_or_create(full_title)
    if not issue_id:
        return False, "find/create issue failed"

    ok = client.update(issue_id, rendered)
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
