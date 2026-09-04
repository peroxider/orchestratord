"""IM Message Gateway server-side package.

Unified IM entry point: inbound dispatch, session routing, capability
gated outbound delivery, and file-based reliability. The gateway daemon
process hosts this service and exposes it to REPL/orchestrator opt-in
clients over POSIX UDS.

v1 (P0–P5) ships: capability contract + registry, gateway skeleton,
WeChat iLink text closed-loop, Orchestrator event push, reliability
hardening, and six-class message semantics (``orchestratord.im_gateway.semantics``).
"""

from __future__ import annotations

from orchestratord.channels.results import CircuitState
from orchestratord.ipc.models import (
    AckLayer,
    AckReceipt,
    InboundMessage,
    MessageSemantics,
    OriginKey,
    OutboundMessage,
    SessionTarget,
)
from orchestratord.ipc.protocol import PROTOCOL_VERSION, FrameType, GatewayFrame

from .binding import BindingEntry, BindingPolicy
from .capability_gate import CapabilityGate
from .config import (
    CommandAllowlistConfig,
    GatewayConfig,
    ReliabilityConfig,
    load_config,
    save_config,
)
from .dispatcher import InboundDispatcher
from .gateway import MessageGateway
from .outbound import OutboundDispatcher
from .processing_status import ProcessingStatusManager
from .reliability import ReliabilityStore
from .router import SessionRouter
from .store import ReliabilityStore as _Store  # noqa: F401
from .text import maybe_truncate_with_liveview, split_text, strip_markdown

__all__ = [
    "PROTOCOL_VERSION",
    "AckLayer",
    "AckReceipt",
    "BindingEntry",
    "BindingPolicy",
    "CapabilityGate",
    "CircuitState",
    "CommandAllowlistConfig",
    "FrameType",
    "GatewayConfig",
    "GatewayFrame",
    "InboundDispatcher",
    "InboundMessage",
    "MessageGateway",
    "MessageSemantics",
    "OriginKey",
    "OutboundDispatcher",
    "OutboundMessage",
    "ProcessingStatusManager",
    "ReliabilityConfig",
    "ReliabilityStore",
    "SessionRouter",
    "SessionTarget",
    "load_config",
    "maybe_truncate_with_liveview",
    "save_config",
    "split_text",
    "strip_markdown",
]
