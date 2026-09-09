"""Session router — resolves an origin to its unique active target.

Resolution order:
  1. opt-in :class:`BindingPolicy` binding (REPL/orchestrator)
  2. Gateway-hosted default auto session (created lazily; full host
     contract lands in P5 — v1 returns a default target placeholder)
"""

from __future__ import annotations

from orchestratord.ipc.models import OriginKey, SessionTarget

from .binding import BindingPolicy


class SessionRouter:
    def __init__(self, binding_policy: BindingPolicy, store=None) -> None:
        self._binding = binding_policy

    def route(self, origin: OriginKey | str) -> SessionTarget:
        key = str(origin)
        # 1. opt-in binding overrides the default route.
        entry = self._binding.get(key)
        if entry is not None:
            return entry.target
        # 2. default Gateway-hosted auto session.
        return SessionTarget(session_id=f"im:default:{key}", host_type="default")

    def is_opt_in(self, origin: OriginKey | str) -> bool:
        return self._binding.is_opt_in(str(origin))

    def is_offline(self, origin: OriginKey | str) -> bool:
        """True if the origin's opt-in target is bound but offline."""
        entry = self._binding.get(str(origin))
        return entry is not None and entry.connection_state == "offline"


__all__ = ["SessionRouter"]
