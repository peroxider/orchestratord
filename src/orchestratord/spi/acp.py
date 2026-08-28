"""SPI: AcpFrontend — ACP server protocol for orchestratord.

Exposes orchestratord as an ACP (Agent Communication Protocol) service
so that ACP-compatible clients (Zed, dsh-acp, etc.) can drive
orchestratord sessions.  This is the reverse direction of the
AcpBackend which wraps ACP services as orchestratord backends.

Per-service capability negotiation is mandatory (§6.9): each ACP
server may support different subsets of resume/delta/live-tool-activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class AcpServerInfo:
    """Metadata reported by an ACP server."""

    name: str = "orchestratord"
    version: str = "0.1.0"
    capabilities: list[str] = field(default_factory=list)


@dataclass
class AcpSessionRequest:
    """Incoming request to create or resume an ACP session."""

    prompt: str | None = None
    session_id: str | None = None
    model: str | None = None
    tools_allow: list[str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AcpFrontend(Protocol):
    """Protocol for exposing orchestratord as an ACP server.

    Implementations handle the transport layer (stdio, HTTP, WebSocket)
    and delegate session management to the orchestratord core.
    """

    def server_info(self) -> AcpServerInfo:
        """Return server metadata for ACP initialize handshake."""
        ...

    async def create_session(self, request: AcpSessionRequest) -> str:
        """Create a new session, return its session_id."""
        ...

    async def cancel_session(self, session_id: str) -> None:
        """Cancel/interrupt a running session."""
        ...

    async def request_permission(
        self, session_id: str, request_id: str, decision: str
    ) -> None:
        """Respond to a pending permission request from a session."""
        ...

    async def close(self) -> None:
        """Graceful shutdown."""
        ...
