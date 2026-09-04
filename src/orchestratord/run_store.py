"""Small JSON run store used by the generic CLI control plane."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunRecord:
    run_id: str
    workflow: str
    task_id: str
    task_kind: str
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    completed_stages: int = 0
    total_stages: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        # A run created by the pre-conversation store is still queryable as a
        # one-run conversation without a migration pass.
        if self.conversation_id is None:
            self.conversation_id = self.run_id


class RunStore:
    def __init__(self, workspace: str | Path) -> None:
        self.root = Path(workspace).expanduser().resolve() / ".orchestratord" / "runs"

    def save(self, record: RunRecord) -> None:
        directory = self.root / record.run_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "run.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        temporary.replace(target)

    def get(self, run_id: str) -> RunRecord | None:
        path = self.root / run_id / "run.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("conversation_id", run_id)
        return RunRecord(**data)

    def list(self, status: str | None = None) -> list[RunRecord]:
        records: list[RunRecord] = []
        if not self.root.exists():
            return records
        for path in self.root.glob("*/run.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data.setdefault("conversation_id", path.parent.name)
                record = RunRecord(**data)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if status is None or record.status == status:
                records.append(record)
        return sorted(records, key=lambda record: record.started_at, reverse=True)

    def set_status(self, run_id: str, status: str) -> RunRecord | None:
        record = self.get(run_id)
        if record is None:
            return None
        record.status = status
        self.save(record)
        return record
