"""Issue registry: persistent issue→commit→PR mapping, stored as JSON.

Split from the single ``issue_registry.py`` into a package by concern:

  - :mod:`orchestratord.issue_registry.models` — ``IssueStatus``
    / ``IssueRecord`` / ``TERMINAL_STATUSES``.
  - :mod:`orchestratord.issue_registry.storage` — JSON
    load / save / throttled diagnostics flush (``StorageMixin``).
  - :mod:`orchestratord.issue_registry.state_machine` —
    queries + lifecycle transitions (``StateMachineMixin``).
  - :mod:`orchestratord.issue_registry.clarification` —
    clarification-field mutations (``ClarificationMixin``).
  - :mod:`orchestratord.issue_registry.feedback` — review
    feedback bookkeeping (``FeedbackMixin``).
  - :mod:`orchestratord.issue_registry.intent` — operator
    intent / retry / rebase-conflict / unblock (``IntentMixin``).

The public class below composes the mixins; the external API surface is
unchanged (``IssueRegistry`` / ``IssueStatus`` / ``IssueRecord`` /
``TERMINAL_STATUSES``).
"""

from __future__ import annotations

from pathlib import Path

from .clarification import ClarificationMixin
from .feedback import FeedbackMixin
from .intent import IntentMixin
from .models import TERMINAL_STATUSES, IssueRecord, IssueStatus
from .state_machine import StateMachineMixin
from .storage import StorageMixin

__all__ = [
    "TERMINAL_STATUSES",
    "IssueRecord",
    "IssueRegistry",
    "IssueStatus",
]


class IssueRegistry(
    StorageMixin,
    StateMachineMixin,
    ClarificationMixin,
    FeedbackMixin,
    IntentMixin,
):
    """Persistent issue→commit→PR mapping, stored as JSON."""

    # High-frequency diagnostics writes (turn/tool counts, last event)
    # coalesce to at most one disk write per this interval. Status / PR
    # mutations are never throttled — they always persist immediately.
    _DIAGNOSTICS_MIN_SAVE_INTERVAL_S = 2.0

    def __init__(
        self,
        storage_path: Path,
        *,
        diagnostics_min_save_interval_s: float | None = None,
    ) -> None:
        """Initialize an empty registry backed by ``storage_path``.

        Args:
            storage_path: path to the registry JSON file.
            diagnostics_min_save_interval_s: minimum interval between
                throttled diagnostics writes (default 2.0s).
        """
        self._path = storage_path
        self._records: dict[str, IssueRecord] = {}
        self._diagnostics_min_save_interval_s = (
            diagnostics_min_save_interval_s
            if diagnostics_min_save_interval_s is not None
            else self._DIAGNOSTICS_MIN_SAVE_INTERVAL_S
        )
        # Throttle bookkeeping for ``_save_diagnostics``.
        self._last_diagnostics_save_monotonic = 0.0
        self._pending_diagnostics_save = False
        self._load()
