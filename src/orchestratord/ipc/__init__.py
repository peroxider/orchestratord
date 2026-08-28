"""Self-contained IM gateway IPC module for orchestratord.

Implements the UDS JSONL protocol, frame types, and message models without
depending on a concrete agent backend.
"""

from orchestratord.ipc.protocol import (
    GatewayFrame,
    GatewayIpcError,
    FrameType,
    PROTOCOL_VERSION,
    constant_time_eq,
)
from orchestratord.ipc.models import (
    InboundMessage,
    OutboundMessage,
    MessageSemantics,
    AckReceipt,
    FEISHU_DM_ALL_ORIGIN,
    IM_DIRECT_ALL_ORIGIN,
)
from orchestratord.ipc.client import GatewayIpcClient

__all__ = [
    "GatewayFrame",
    "GatewayIpcClient",
    "GatewayIpcError",
    "FrameType",
    "PROTOCOL_VERSION",
    "constant_time_eq",
    "InboundMessage",
    "OutboundMessage",
    "MessageSemantics",
    "AckReceipt",
    "FEISHU_DM_ALL_ORIGIN",
    "IM_DIRECT_ALL_ORIGIN",
]
