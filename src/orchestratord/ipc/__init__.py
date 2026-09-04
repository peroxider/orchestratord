"""Self-contained IM gateway IPC module for orchestratord.

Implements the UDS JSONL protocol, frame types, and message models without
depending on a concrete agent backend.
"""

from orchestratord.ipc.client import GatewayIpcClient
from orchestratord.ipc.models import (
    FEISHU_DM_ALL_ORIGIN,
    IM_DIRECT_ALL_ORIGIN,
    WECHAT_DIRECT_ALL_ORIGIN,
    AckLayer,
    AckReceipt,
    InboundMessage,
    MessageSemantics,
    OriginKey,
    OutboundMessage,
    SessionTarget,
)
from orchestratord.ipc.protocol import (
    PROTOCOL_VERSION,
    FrameType,
    GatewayFrame,
    GatewayIpcError,
    constant_time_eq,
)

__all__ = [
    "FEISHU_DM_ALL_ORIGIN",
    "IM_DIRECT_ALL_ORIGIN",
    "PROTOCOL_VERSION",
    "WECHAT_DIRECT_ALL_ORIGIN",
    "AckLayer",
    "AckReceipt",
    "FrameType",
    "GatewayFrame",
    "GatewayIpcClient",
    "GatewayIpcError",
    "InboundMessage",
    "MessageSemantics",
    "OriginKey",
    "OutboundMessage",
    "SessionTarget",
    "constant_time_eq",
]
