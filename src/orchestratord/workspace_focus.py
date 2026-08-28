"""Deterministic local workspace-focus computation."""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath


def compute_workspace_focuses(
    *,
    changed_files: list[str],
    recent_messages: list[str] | None = None,
) -> list[dict]:
    """Return a list of focus-area dicts derived from changed-file paths.

    Each dict has ``"focus"`` (str) and ``"confidence"`` (float 0.0–1.0).
    An empty list means no focus could be determined.

    Heuristic: group changed files by top-level directory, rank by file
    count, and return the top 3 areas with confidence proportional to the
    fraction of total changed files.
    """
    if not changed_files:
        return []

    _ = recent_messages  # reserved for future use

    areas: Counter[str] = Counter()
    for path in changed_files:
        parts = PurePosixPath(path).parts
        if parts:
            areas[parts[0]] += 1
        else:
            areas["(root)"] += 1

    total = sum(areas.values())
    if total == 0:
        return []

    result: list[dict] = []
    for area, count in areas.most_common(3):
        result.append({
            "focus": area,
            "confidence": round(count / total, 2),
        })
    return result
