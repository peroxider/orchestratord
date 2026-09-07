"""Live-session registry (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.3).

In-process map of active :class:`orchestratord.spi.session.AgentSession`
handles keyed by the orchestratord session id. The sessions router
(``orchestratord.api.routers.sessions``) reaches the running backend
through this registry to forward operator decisions: ``approve``/``deny``
go to ``AgentSession.approve``; ``stop``/``pause``/``resume`` go to
``AgentSession.interrupt`` and/or the per-session
:class:`orchestratord.process_control.ProcessTree`.

Single-instance only — multica's ``server/internal/realtime`` adds a
Redis pub/sub relay for fan-out across server instances. We mirror that
boundary here so swapping in a Redis-backed registry later only changes
the backend, not the consumer API.

The registry is intentionally minimal: a ``dict`` guarded by an
``asyncio.Lock``, with a small dataclass for the per-session payload
so callers don't have to unpack tuples.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestratord.process_control import ProcessTree
    from orchestratord.spi.capabilities import BackendCapabilities
    from orchestratord.spi.session import AgentSession


@dataclass
class LiveSession:
    """Bookkeeping for one in-process agent session.

    ``spi_session`` is the SPI handle the backend returned from
    ``start_session``; it is the conduit for ``approve``/``interrupt``
    calls. ``process_tree`` is optional — only present when the backend
    runs as a child process the operator can pause/resume/stop.
    ``capabilities`` is the snapshot taken when the session was
    registered, so the API layer can gate operations on the 8-bit matrix
    without re-asking the backend.
    """

    spi_session: "AgentSession"
    capabilities: "BackendCapabilities"
    process_tree: "ProcessTree | None" = None
    metadata: dict[str, Any] = field(default_factory=dict)


class LiveSessionRegistry:
    """Process-local registry of :class:`LiveSession` records.

    The orchestrator populates this map as it starts and finishes
    sessions; the API layer reads from it to forward operator decisions.
    All operations are coroutine-safe (the lock makes publish/get
    linearizable), but :meth:`get` returns a snapshot reference — callers
    must treat the ``LiveSession`` as immutable.
    """

    def __init__(self) -> None:
        self._by_session_id: dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()

    async def register(self, session_id: str, live: LiveSession) -> None:
        """Add or replace the entry for ``session_id``."""
        async with self._lock:
            self._by_session_id[session_id] = live

    async def unregister(self, session_id: str) -> None:
        """Remove the entry for ``session_id`` (no-op if absent)."""
        async with self._lock:
            self._by_session_id.pop(session_id, None)

    async def get(self, session_id: str) -> LiveSession | None:
        """Return the live session for ``session_id``, or ``None`` if it
        is not currently running in this process (e.g. it lives in a
        sibling daemon — Phase B will bridge that)."""
        async with self._lock:
            return self._by_session_id.get(session_id)

    async def ids(self) -> list[str]:
        """Return a snapshot of registered session ids (for diagnostics)."""
        async with self._lock:
            return list(self._by_session_id.keys())
