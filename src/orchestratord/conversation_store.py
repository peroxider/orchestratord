"""Durable logical-conversation index.

Transcripts remain append-only and are stored per run.  This small JSON
manifest is the stable join between those transcripts and intentionally does
not contain provider-specific resume state.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


def ensure_conversation_id(existing: str | None = None) -> str:
    """Return *existing* or create a new UUID-shaped conversation id."""
    return existing or str(uuid.uuid4())


class ConversationStore:
    """Atomic JSON manifests under ``<base>/conversations``.

    ``base`` is normally ``~/.orchestratord``.  Keeping it injectable makes
    the store straightforward to use in tests and by embedded daemons.
    """

    def __init__(self, base: str | Path | None = None) -> None:
        self.base = Path(base).expanduser() if base is not None else Path.home() / ".orchestratord"
        self.root = self.base / "conversations"

    def _path(self, conversation_id: str) -> Path:
        if not conversation_id or "/" in conversation_id or "\\" in conversation_id:
            raise ValueError("invalid conversation_id")
        return self.root / conversation_id / "conversation.json"

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(conversation_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        result: list[dict[str, Any]] = []
        for path in self.root.glob("*/conversation.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                result.append(data)
        return sorted(result, key=lambda item: float(item.get("updated_at") or 0), reverse=True)

    def register_run(
        self,
        *,
        conversation_id: str,
        run_id: str,
        backend: str | None = None,
        backend_session_id: str | None = None,
        issue_id: str | None = None,
        parent_run_id: str | None = None,
        stage_id: str | None = None,
        stage_name: str | None = None,
        branch_id: str | None = None,
        status: str = "running",
        started_at: float | None = None,
    ) -> dict[str, Any]:
        """Create or update the manifest entry for one run idempotently."""
        now = time.time()
        manifest = self.get(conversation_id) or {
            "schema_version": 1,
            "conversation_id": conversation_id,
            "issue_id": issue_id,
            "created_at": started_at or now,
            "updated_at": now,
            "status": status,
            "runs": [],
        }
        if issue_id and not manifest.get("issue_id"):
            manifest["issue_id"] = issue_id
        runs = manifest.setdefault("runs", [])
        entry = next((item for item in runs if item.get("run_id") == run_id), None)
        if entry is None:
            entry = {"run_id": run_id}
            runs.append(entry)
        values = {
            "backend": backend,
            "backend_session_id": backend_session_id,
            "parent_run_id": parent_run_id,
            "stage_id": stage_id,
            "stage_name": stage_name,
            "branch_id": branch_id,
            "started_at": started_at or entry.get("started_at") or now,
            "status": status,
        }
        for key, value in values.items():
            if value is not None:
                entry[key] = value
        entry.setdefault("finished_at", None)
        manifest["updated_at"] = now
        manifest["status"] = status
        self._save(conversation_id, manifest)
        return manifest

    def update_run(
        self,
        conversation_id: str,
        run_id: str,
        *,
        status: str | None = None,
        finished_at: float | None = None,
        backend_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        manifest = self.get(conversation_id)
        if manifest is None:
            return None
        entry = next((item for item in manifest.get("runs", []) if item.get("run_id") == run_id), None)
        if entry is None:
            return None
        if status is not None:
            entry["status"] = status
        if finished_at is not None:
            entry["finished_at"] = finished_at
        if backend_session_id is not None:
            entry["backend_session_id"] = backend_session_id
        manifest["updated_at"] = time.time()
        if status is not None:
            manifest["status"] = "running" if any(
                item.get("status") == "running" for item in manifest.get("runs", [])
            ) else status
        self._save(conversation_id, manifest)
        return manifest

    def ensure_legacy_run(self, run_id: str, *, issue_id: str | None = None) -> dict[str, Any] | None:
        """Expose a pre-v2 run as a one-run conversation on first access."""
        if not run_id or "/" in run_id or "\\" in run_id:
            return None
        existing = self.get(run_id)
        if existing is not None:
            return existing
        transcript = self.base / "sessions" / run_id / "transcript.jsonl"
        if not transcript.exists():
            return None
        return self.register_run(
            conversation_id=run_id,
            run_id=run_id,
            issue_id=issue_id,
            status="completed",
        )

    def _save(self, conversation_id: str, manifest: dict[str, Any]) -> None:
        target = self._path(conversation_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
