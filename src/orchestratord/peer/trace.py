"""Cross-daemon trace correlation for the peer federation.

A trace id is a 32-hex string that rides:

* the peer/1 frame path — ``headers["x-trace-id"]`` on an INVOKE,
  echoed in the RESULT frame's headers;
* the REST peer path — the ``X-Trace-Id`` HTTP request header, echoed
  in the response.

Both inbound surfaces bind the id into a :class:`contextvars.ContextVar`
so log lines emitted while dispatching that request can carry it;
:meth:`orchestratord.peer.client.PeerClient.invoke` stamps a fresh id
when the caller has none, so every cross-daemon hop is followable
end-to-end even without an upstream tracing system.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Any, Mapping

TRACE_HEADER = "x-trace-id"

_current_trace_id: ContextVar[str | None] = ContextVar(
    "peer_trace_id", default=None
)


def new_trace_id() -> str:
    """A fresh 32-hex trace id."""
    return uuid.uuid4().hex


def current_trace_id() -> str | None:
    """The ambient trace id for this async context (None when unset)."""
    return _current_trace_id.get()


def set_current_trace_id(trace_id: str) -> Token[str | None]:
    """Bind the trace id for the current context; reset with the token."""
    return _current_trace_id.set(trace_id)


def reset_current_trace_id(token: Token[str | None]) -> None:
    _current_trace_id.reset(token)


def resolve_trace_id(trace_id: str | None = None) -> str:
    """Explicit id → ambient id → fresh id, in that order."""
    return trace_id or current_trace_id() or new_trace_id()


def trace_id_from_headers(headers: Mapping[str, Any] | None) -> str | None:
    """Case-insensitive ``x-trace-id`` lookup (frame headers use it)."""
    if not headers:
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == TRACE_HEADER and value:
            return str(value)
    return None


__all__ = [
    "TRACE_HEADER",
    "current_trace_id",
    "new_trace_id",
    "reset_current_trace_id",
    "resolve_trace_id",
    "set_current_trace_id",
    "trace_id_from_headers",
]
