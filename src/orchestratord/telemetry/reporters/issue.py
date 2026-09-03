"""Telemetry reporter — push the daily summary to a GitCode issue.

Find-or-create an issue whose title matches ``issue_title`` in the
configured repo, then update its body with the day's Markdown summary.
Cursor-based dedupe: the last reported day is persisted next to the events
store, so a day is only pushed once per (owner, repo, title).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..storage import telemetry_dir

_CURSOR_FILENAME = "report_cursor.json"


def _cursor_path() -> Path:
    return telemetry_dir() / _CURSOR_FILENAME


def _read_cursor() -> dict[str, str]:
    p = _cursor_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_cursor(key: str, day: str) -> None:
    cur = _read_cursor()
    cur[key] = day
    telemetry_dir().mkdir(parents=True, exist_ok=True)
    _cursor_path().write_text(
        json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
    # 约定：每个用户每天一个 issue —— 标题带日期（每天一个独立 issue，
    # 旧天的数据保留在当天的 issue 里，不被后续日期覆盖）。
    full_title = f"{title} {day}" if day not in title else title
    key = f"{owner}/{repo}:{full_title}"
    cursor = _read_cursor()
    if not force and cursor.get(key) == day:
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


def cursor_for(owner: str, repo: str, title: str) -> str:
    return _read_cursor().get(f"{owner}/{repo}:{title}", "")
