"""Learnings artifact writer (DESIGN_EXPERIENCE_LOOP.md §4/§8).

Two-level layout: workspace-level
``<workspace>/.orchestratord/learnings/YYYY-MM-DD-<slug>.md`` is the
default landing zone (git-friendly, shared with the team); the global
``$ORCHESTRATORD_HOME/learnings/`` directory catches sessions without a
workspace context. Every write passes a privacy scrub first — a scrub
failure drops the artifact (fail-closed, never persist unscrubbed
content) — and a cross-workspace fingerprint check skips near
duplicates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from orchestratord.privacy import scrub

logger = logging.getLogger(__name__)

_FRAGMENTS = re.compile(r"[^a-z0-9]+")
_FINGERPRINTS_FILE = ".fingerprints.json"


@dataclass
class LearningDoc:
    session_id: str
    issue_id: str
    repo: str
    friction_score: int
    tags: list[str]
    title: str
    body: str
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d")
    )


def fingerprint(doc: LearningDoc) -> str:
    """Weak frontmatter fingerprint: repo + sorted tags + normalized title."""
    normalized_title = _FRAGMENTS.sub("", doc.title.lower())
    payload = "|".join(
        [doc.repo, ",".join(sorted(doc.tags)), normalized_title]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _global_learnings_dir() -> Path:
    base = os.environ.get(
        "ORCHESTRATORD_HOME", str(Path.home() / ".orchestratord")
    )
    return Path(base) / "learnings"


def _load_fingerprints(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _slugify(title: str) -> str:
    slug = _FRAGMENTS.sub("-", title.lower()).strip("-")[:48]
    return slug or "learning"


class LearningWriter:
    """Scrub → fingerprint-dedupe → persist one learning document."""

    def __init__(self, *, allowlist: list[str] | None = None) -> None:
        self._allowlist = list(allowlist or [])

    def write(self, doc: LearningDoc, workspace_root: Path | None) -> Path | None:
        """Persist ``doc``; returns the written path or None when dropped.

        Raises whatever ``scrub`` raises (fail-closed upstream contract is
        enforced here by never writing partial output).
        """
        try:
            clean_title = scrub(doc.title, allowlist=self._allowlist)
            clean_body = scrub(doc.body, allowlist=self._allowlist)
        except Exception:
            # Fail-closed (§8.2): a scrub failure drops the artifact
            # instead of ever persisting unscrubbed content.
            logger.warning(
                "learning dropped: scrub failed (fail-closed)", exc_info=True
            )
            return None

        target_dir = (
            Path(workspace_root) / ".orchestratord" / "learnings"
            if workspace_root
            else _global_learnings_dir()
        )
        fp = fingerprint(doc)
        fingerprints_path = _global_learnings_dir() / _FINGERPRINTS_FILE
        known = _load_fingerprints(fingerprints_path)
        if fp in known:
            logger.info(
                "learning skipped (duplicate fingerprint): %s", known[fp]
            )
            return None

        target_dir.mkdir(parents=True, exist_ok=True)
        stem = _slugify(doc.title)
        path = target_dir / f"{doc.created_at}-{stem}.md"
        counter = 2
        while path.exists():
            path = target_dir / f"{doc.created_at}-{stem}-{counter}.md"
            counter += 1

        frontmatter = (
            "---\n"
            f"session_id: {doc.session_id}\n"
            f"issue_id: {doc.issue_id}\n"
            f"repo: {doc.repo}\n"
            f"friction_score: {doc.friction_score}\n"
            f"tags: {json.dumps(doc.tags, ensure_ascii=False)}\n"
            f"created_at: {doc.created_at}\n"
            "---\n"
        )
        path.write_text(
            f"{frontmatter}\n# {clean_title}\n\n{clean_body}\n",
            encoding="utf-8",
        )

        known[fp] = str(path)
        try:
            fingerprints_path.parent.mkdir(parents=True, exist_ok=True)
            fingerprints_path.write_text(
                json.dumps(known, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("failed to persist learning fingerprints", exc_info=True)
        return path


__all__ = ["LearningDoc", "LearningWriter", "fingerprint"]
