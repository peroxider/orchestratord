"""Normalized issue representation used by the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Issue:
    """Normalized issue used by the orchestrator."""

    id: str | None = None
    identifier: str | None = None
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    state: str | None = None
    branch_name: str | None = None
    url: str | None = None
    assignee_id: str | None = None
    author_login: str | None = None
    blocked_by: list[dict[str, Any]] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    assigned_to_worker: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Per-issue python interpreter override (cascade level 1,
    # highest priority). When non-empty, this value overrides every
    # other source — workspace ``python_executable``,
    # ``_detect_python_in_workspace``, and ``agent.python_executable``.
    # Empty string (the default) means "fall through to the next
    # level of the cascade." Populated by ``LocalTrackerAdapter`` from
    # the issue markdown frontmatter (``python_executable: ...``);
    # remote trackers (GitHub/Gitee/GitCode/Linear) leave it empty
    # and rely on workspace-level or agent-level configuration.
    python_executable: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert the issue to a plain dict for template rendering.

        Datetime fields are serialized to ISO-8601 strings so the result
        is JSON-friendly.
        """
        result: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result

    def label_names(self) -> list[str]:
        """Return a copy of the issue's label names."""
        return list(self.labels)
