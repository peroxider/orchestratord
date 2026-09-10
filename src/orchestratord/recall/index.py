"""Recall index — scan learnings / rules / local issues into RecDocs.

Read-only, rebuild-per-call (no persistent inverted index in v1;
document volumes are small and an mtime cache is deferred to P1).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from orchestratord.rules_learner import RuleStore

logger = logging.getLogger(__name__)


@dataclass
class RecDoc:
    doc_id: str
    kind: str  # learning | rule | issue
    repo: str
    tags: list[str] = field(default_factory=list)
    title: str = ""
    body: str = ""
    mtime: float = 0.0
    source_path: str = ""


def _global_learnings_dir() -> Path:
    base = os.environ.get(
        "ORCHESTRATORD_HOME", str(Path.home() / ".orchestratord")
    )
    return Path(base) / "learnings"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading YAML-ish frontmatter block; values stay raw strings."""
    if not text.startswith("---"):
        return {}, text
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}, text
    header = text[3:end].strip()
    body = text[end + 4 :]
    meta: dict[str, object] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "tags":
            try:
                parsed = json.loads(value)
                meta["tags"] = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                meta["tags"] = []
        else:
            meta[key] = value
    return meta, body


def _learning_docs(directory: Path, prefix: str, seen: set[str]) -> list[RecDoc]:
    docs: list[RecDoc] = []
    if not directory.is_dir():
        return docs
    for path in sorted(directory.glob("*.md")):
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            meta, body = _parse_frontmatter(
                path.read_text(encoding="utf-8", errors="replace")
            )
            mtime = path.stat().st_mtime
        except OSError:
            continue
        title = ""
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        docs.append(
            RecDoc(
                doc_id=f"{prefix}:{path}",
                kind="learning",
                repo=str(meta.get("repo", "") or ""),
                tags=[str(t) for t in meta.get("tags", [])],
                title=title or path.stem,
                body=body,
                mtime=mtime,
                source_path=str(path),
            )
        )
    return docs


def _rule_docs() -> list[RecDoc]:
    from orchestratord.workflow_store import get_workflow_store

    workflow_path = get_workflow_store().workflow_path
    if not workflow_path:
        return []
    try:
        data = RuleStore.load(workflow_path)
    except Exception:
        logger.debug("rules store unreadable for recall", exc_info=True)
        return []
    docs: list[RecDoc] = []
    for rule in data.get("rules", []):
        summary = str(rule.get("summary", ""))
        body = str(rule.get("body", ""))
        docs.append(
            RecDoc(
                doc_id=f"rule:{rule.get('id', summary)}",
                kind="rule",
                repo="",
                tags=[str(rule.get("category", ""))],
                title=summary,
                body=f"{summary}\n{body}".strip(),
                mtime=0.0,
                source_path=str(workflow_path),
            )
        )
    return docs


def _issue_docs(issues_dir: Path | None) -> list[RecDoc]:
    docs: list[RecDoc] = []
    if issues_dir is None or not issues_dir.is_dir():
        return docs
    for path in sorted(issues_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mtime = path.stat().st_mtime
        except OSError:
            continue
        title = ""
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        docs.append(
            RecDoc(
                doc_id=f"issue:{path.stem}",
                kind="issue",
                repo=path.stem,
                tags=[],
                title=title or path.stem,
                body=text,
                mtime=mtime,
                source_path=str(path),
            )
        )
    return docs


def build_index(
    workspace_root: Path | str | None = None,
    issues_dir: Path | str | None = None,
) -> list[RecDoc]:
    """Collect documents from every recall source.

    Sources: workspace-level learnings, global learnings, the workflow
    rules store (annotated "约定" at search time), and — when
    configured — local tracker issue markdown files.
    """
    seen: set[str] = set()
    docs: list[RecDoc] = []
    if workspace_root is not None:
        docs.extend(
            _learning_docs(
                Path(workspace_root) / ".orchestratord" / "learnings",
                "ws-learning",
                seen,
            )
        )
    docs.extend(_learning_docs(_global_learnings_dir(), "global-learning", seen))
    docs.extend(_rule_docs())
    docs.extend(_issue_docs(Path(issues_dir) if issues_dir else None))
    return docs


__all__ = ["RecDoc", "build_index"]
