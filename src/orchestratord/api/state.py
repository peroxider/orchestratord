"""Process-wide :class:`DashboardState` provider for the API compat layer.

The legacy ``cli/dashboard.py`` binds one ``DashboardState`` to the handler
class inside ``run()``.  The FastAPI app has no ``run()``, so it lazily builds
a single shared state here and reuses it across the compat routes.  This keeps
the dashboard read model, event tailer, and chat gateway single-sourced
instead of forking a fresh copy per request.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_state: Any | None = None


def get_dashboard_state() -> Any:
    """Return the shared DashboardState, constructing it on first use."""
    global _state
    with _lock:
        if _state is None:
            from orchestratord.cli.dashboard import (
                DashboardState,
                _resolve_workspace_root,
            )

            _state = DashboardState(_resolve_workspace_root())
        return _state
