"""SPI — Service Provider Interface for orchestratord backends.

All protocol definitions in this package are frozen: additive changes
only — new capability bits, new event kinds; no semantic changes.
Breaking changes require a 2.0 revision.
"""

from orchestratord.spi.acp import (
    AcpFrontend,
    AcpServerInfo,
    AcpSessionRequest,
)
from orchestratord.spi.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
)
from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import AgentSession, ResumeStatus, SessionResult

__all__ = [
    "AcpFrontend",
    "AcpServerInfo",
    "AcpSessionRequest",
    "AgentBackend",
    "AgentSession",
    "ApprovalDecision",
    "ApprovalPolicy",
    "ApprovalRequest",
    "BackendCapabilities",
    "BackendDescriptor",
    "BackendFamily",
    "EventEnvelope",
    "EventKind",
    "ResumeStatus",
    "SessionResult",
    "SessionSpec",
]