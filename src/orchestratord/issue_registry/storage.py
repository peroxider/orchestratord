"""JSON persistence for the issue registry (split from ``issue_registry.py``)."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from ..tracker import Intent
from .models import IssueRecord, IssueStatus

logger = logging.getLogger(__name__)


class StorageMixin:
    """JSON persistence: load / save / throttled diagnostics flush.

    The host class provides ``_path`` and ``_records`` (initialized in its
    ``__init__``) plus the throttle bookkeeping fields
    ``_diagnostics_min_save_interval_s`` / ``_last_diagnostics_save_monotonic``
    / ``_pending_diagnostics_save``.
    """

    def flush(self) -> None:
        """Persist any pending throttled diagnostics write.

        Call at the end of a run (including paths that don't end in a
        durable status mutation, e.g. ``pending_review``) so the final
        diagnostics snapshot reaches disk.
        """
        if self._pending_diagnostics_save:
            self._save()

    def _load(self) -> None:
        """Load registry records from the JSON file on disk.

        Missing files are treated as an empty registry; files with a
        newer ``schema_version`` are rejected loudly, and status/intent
        strings are converted back to their enum values.  Strict
        construction means unknown values or fields raise instead of
        being silently dropped.
        """
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._records = {}
        for k, v in data.items():
            v = dict(v)
            file_version = int(v.pop("schema_version", 1))
            if file_version > IssueRecord.schema_version:
                raise ValueError(
                    f"registry {self._path} schema_version={file_version} "
                    f"newer than supported {IssueRecord.schema_version}; "
                    "refusing to load (upgrade the code first)"
                )
            # Convert status / intent strings to their enum values. Strict:
            # an unknown value raises instead of silently falling back.
            if isinstance(v.get("status"), str):
                v["status"] = IssueStatus(v["status"])
            if isinstance(v.get("intent"), str):
                v["intent"] = Intent(v["intent"])
            # Strict construction: any key not on IssueRecord raises a
            # TypeError, so a corrupted / foreign registry file surfaces
            # loudly instead of being silently dropped.
            self._records[k] = IssueRecord(**v)

    def _save(self) -> None:
        """Atomically persist all records to the JSON file.

        Writes to a temp file in the same directory, then moves it into
        place with ``os.replace``.  On failure the temp file is cleaned
        up and a warning is logged; throttle bookkeeping is only reset
        on success so a failed write does not swallow a pending
        diagnostics flush.
        """
        tmp_path: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {k: asdict(v) for k, v in self._records.items()},
                indent=2,
                ensure_ascii=False,
            )
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(payload)
            tmp_path.replace(self._path)
        except Exception as exc:
            if tmp_path is not None:
                with suppress(OSError):
                    tmp_path.unlink()
            logger.warning("Failed to save issue registry: %s", exc)
            return
        # A durable write persists the latest in-memory state — including
        # any diagnostics that were only marked pending — so reset the
        # throttle window. Done only on success so a failed write doesn't
        # silently swallow a pending diagnostics flush.
        self._pending_diagnostics_save = False
        self._last_diagnostics_save_monotonic = time.monotonic()

    def _save_diagnostics(self) -> None:
        """Throttled save for high-frequency observational updates.

        ``update_run_diagnostics`` fires on every agent event (turn/tool
        counts, last event/tool). Rewriting the whole registry on each
        one thrashes the disk on a busy run. Coalesce to at most one
        write per ``_diagnostics_min_save_interval_s``; the latest state
        is always in memory, and any durable mutation (status / PR
        change) or an explicit :meth:`flush` persists pending data. On a
        crash we lose at most one interval's worth of *observational*
        data — never status/PR state, which still saves immediately.
        """
        now = time.monotonic()
        if now - self._last_diagnostics_save_monotonic >= self._diagnostics_min_save_interval_s:
            self._save()  # resets pending flag + timestamp on success
        else:
            self._pending_diagnostics_save = True
