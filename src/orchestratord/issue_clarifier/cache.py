"""Fingerprint + persistent JSON cache for issue-clarifier results.

The cache is keyed by :func:`build_fingerprint` hashes so that identical
issue content (plus identical prior clarification replies) skips a
redundant LLM round-trip. Results are persisted as JSON next to the
workspace (``.orchestratord_issue_clarifier_cache.json`` by default).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from ..issue import Issue
    from .models import ClarifyResult

logger = logging.getLogger(__name__)

__all__ = ["ClarifierCache", "build_fingerprint"]


def build_fingerprint(
    issue: "Issue",
    *,
    prior_replies: Iterable[str] = (),
    version: str = "v1",
) -> str:
    """Return a stable hash of the issue content and reply history.

    The fingerprint changes whenever anything that could alter the
    clarifier's answer changes: title, description, priority, state,
    dependencies, labels, or any prior clarification reply.
    """
    payload = {
        "version": version,
        "id": getattr(issue, "id", None),
        "identifier": getattr(issue, "identifier", None),
        "title": getattr(issue, "title", None),
        "description": getattr(issue, "description", None),
        "priority": getattr(issue, "priority", None),
        "state": getattr(issue, "state", None),
        "depends_on": [str(item) for item in (getattr(issue, "depends_on", None) or ())],
        "labels": [str(item) for item in (getattr(issue, "labels", None) or ())],
        "prior_replies": [str(reply) for reply in prior_replies],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ClarifierCache:
    """JSON-file-backed cache of :class:`ClarifyResult` entries.

    Parameters
    ----------
    path:
        Location of the JSON cache file (created on first write).
    enabled:
        When ``False`` the cache is pass-through: ``get`` always returns
        ``None`` and ``put`` is a no-op.
    """

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self._path = Path(path)
        self._enabled = enabled
        self._entries: dict[str, dict[str, Any]] = {}
        if self._enabled:
            self._load()

    # ------------------------------------------------------------------
    # Public API (used by IssueClarifierService)
    # ------------------------------------------------------------------

    def get(self, fingerprint: str) -> "ClarifyResult | None":
        """Return the cached result for ``fingerprint``, or ``None``."""
        if not self._enabled:
            return None
        entry = self._entries.get(str(fingerprint))
        if entry is None:
            return None
        from .models import ClarifyResult

        try:
            return ClarifyResult(**entry)
        except Exception as exc:
            logger.warning("Ignoring malformed clarifier cache entry: %s", exc)
            return None

    def put(self, result: "ClarifyResult") -> None:
        """Persist ``result`` under its own fingerprint."""
        if not self._enabled:
            return
        fingerprint = str(getattr(result, "fingerprint", "") or "")
        if not fingerprint:
            return
        try:
            self._entries[fingerprint] = asdict(result)
        except Exception as exc:
            logger.warning("Clarifier cache serialization failed: %s", exc)
            return
        self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Clarifier cache load failed (%s): %s", self._path, exc)
            return
        if isinstance(data, dict):
            self._entries = {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Clarifier cache save failed (%s): %s", self._path, exc)
