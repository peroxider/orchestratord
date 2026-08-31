"""Markdown issue document parsing for the local tracker."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..issue_registry.issue import Issue

logger = logging.getLogger(__name__)

# Canonical YAML frontmatter delimiter. ``write_markdown_frontmatter`` always
# emits exactly three dashes, but ``_find_frontmatter`` accepts any run of
# two-or-more dashes on its own line so that hand-authored issues with the
# common ``--`` typo are still parsed correctly.
_FRONTMATTER_DELIMITER = "---"
_FRONTMATTER_DELIMITER_RE = re.compile(r"^\s*-{2,}\s*$")
_DEFAULT_BRANCH_NAME_TRUNCATE_LENGTH = 48
# 日志中预览文件头的字符数。
_LOG_PREVIEW_CHARS = 80


@dataclass(frozen=True)
class LocalIssueDocument:
    path: Path
    metadata: dict[str, Any]
    body: str
    issue: Issue
    pr_number: str | None = None
    pr_url: str | None = None
    base_branch: str | None = None


def parse_markdown_issue(path: Path) -> LocalIssueDocument:
    """Parse a markdown issue document into a ``LocalIssueDocument``.

    Args:
        path: Path to the markdown issue file.

    Returns:
        The parsed document, including metadata, body and a normalized
        ``Issue`` model.
    """
    text = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(text)
    title, description = _title_and_description(path, metadata, body)
    identifier = _string_or_none(metadata.get("identifier"))
    issue_id = _string_or_none(metadata.get("id")) or identifier or path.stem
    identifier = identifier or issue_id
    branch_name = _string_or_none(metadata.get("branch_name")) or _default_branch_name(
        identifier,
        title,
    )

    issue = Issue(
        id=issue_id,
        identifier=identifier,
        title=title,
        description=description,
        priority=_int_or_none(metadata.get("priority")),
        state=_string_or_none(metadata.get("state")),
        branch_name=branch_name,
        url=_string_or_none(metadata.get("url")) or str(path),
        assignee_id=_string_or_none(metadata.get("assignee_id")),
        depends_on=_string_list(metadata.get("depends_on")),
        labels=_string_list(metadata.get("labels")),
        created_at=_datetime_or_none(metadata.get("created_at")),
        updated_at=_datetime_or_none(metadata.get("updated_at")),
        # Per-issue python interpreter override (cascade level 1,
        # highest priority). Pulled from the issue markdown
        # frontmatter; remote trackers (GitHub/Gitee/GitCode/Linear)
        # leave this empty and rely on workspace-level or agent-level
        # configuration. Empty string falls through to the next
        # cascade level in ``resolve_python_executable``.
        python_executable=_string_or_none(metadata.get("python_executable")) or "",
    )
    return LocalIssueDocument(
        path=path,
        metadata=metadata,
        body=body,
        issue=issue,
        pr_number=_string_or_none(metadata.get("pr_number")),
        pr_url=_string_or_none(metadata.get("pr_url")),
        base_branch=_string_or_none(metadata.get("base_branch")),
    )


def write_markdown_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    """Merge ``updates`` into the frontmatter and atomically rewrite the file.

    Args:
        path: Path to the markdown issue file.
        updates: Frontmatter key/value pairs; None values are skipped.
    """
    text = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(text)
    metadata.update({k: v for k, v in updates.items() if v is not None})
    serialized = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    new_text = f"{_FRONTMATTER_DELIMITER}\n{serialized}\n{_FRONTMATTER_DELIMITER}\n{body}"
    _atomic_write(path, new_text)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a markdown document into (frontmatter metadata, body).

    Tolerant of:
      * Common ``--`` (two-dash) typo in place of the canonical ``---``
        delimiter. Any run of two-or-more dashes on a line by itself
        is accepted as a delimiter (see ``_FRONTMATTER_DELIMITER_RE``).
      * Missing frontmatter entirely (the whole text is the body).
      * Malformed YAML (an empty metadata dict is returned and the
        document is treated as body-only).
    """
    lines = text.splitlines(keepends=True)
    end = _find_frontmatter(lines)
    if end is None:
        return {}, text

    raw = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    try:
        parsed = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        logger.warning(
            "Failed to parse frontmatter (preview: %s): %s",
            _frontmatter_preview(lines, end),
            exc,
        )
        return {}, text
    metadata = parsed if isinstance(parsed, dict) else {}
    logger.debug(
        "Parsed frontmatter preview (%d chars max): %s",
        _LOG_PREVIEW_CHARS,
        _frontmatter_preview(lines, end),
    )
    return metadata, body


def _frontmatter_preview(lines: list[str], end: int) -> str:
    """First ``_LOG_PREVIEW_CHARS`` chars of the raw frontmatter block."""
    raw = "".join(lines[: end + 1])
    return raw[:_LOG_PREVIEW_CHARS]


def _find_frontmatter(lines: list[str]) -> int | None:
    """Return the index of the closing delimiter line, or ``None``.

    The opening delimiter must be the very first line of the document
    and the closing delimiter is the next line that matches the
    delimiter pattern.
    """
    if not lines or not _FRONTMATTER_DELIMITER_RE.match(lines[0]):
        return None
    for index in range(1, len(lines)):
        if _FRONTMATTER_DELIMITER_RE.match(lines[index]):
            return index
    return None


def _title_and_description(
    path: Path,
    metadata: dict[str, Any],
    body: str,
) -> tuple[str, str]:
    """Resolve the issue title and description.

    Priority: explicit ``title`` frontmatter key, then the first
    ``# `` heading in the body (the heading is removed from the body),
    then the file stem.
    """
    metadata_title = _string_or_none(metadata.get("title"))
    if metadata_title:
        return metadata_title, body.strip()

    match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if not match:
        return path.stem, body.strip()

    description = body[: match.start()] + body[match.end() :]
    return match.group(1).strip(), description.strip()


def _default_branch_name(identifier: str, title: str) -> str:
    slug = _slugify(f"{identifier}-{title}")
    return f"local/{slug[:_DEFAULT_BRANCH_NAME_TRUNCATE_LENGTH]}"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-._")
    return slug or "issue"


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _datetime_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
        Path(tmp_name).replace(path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise