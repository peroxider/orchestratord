"""Channels service primitives (contract layer).

This package ships the cross-platform ``BaseChannel`` ABC, a thread-safe
``ChannelManager`` dispatcher, a minimal async HTTP transport with
webhook URL safety checks, the capability-based channel adapter
contract (``ChannelAdapter`` / ``ChannelCapabilitySet``), and the unified
``ChannelAdapterRegistry``.

Concrete IM adapters (webhook channels such as Feishu/Slack/Discord and
app-based channels such as Feishu websocket and WeChat iLink) are wired in
by later migration phases: they must register factories through the
registry and keep imports of heavy optional dependencies inside those
factories so this package imports cleanly in a base environment.
"""

from __future__ import annotations

from .base import BaseChannel, ChannelManager
from .capabilities import (
    CapabilityDescriptor,
    CapabilityNotDeclaredError,
    CardUpdateCapability,
    ChannelAdapter,
    ChannelCapability,
    ChannelCapabilitySet,
    InboundActivityContext,
    OutboundCapability,
    ProcessingOutcome,
    ProcessingStatusCapability,
)
from .exceptions import (
    ChannelDisabledError,
    ChannelError,
    ChannelNotFoundError,
    InvalidWebhookURLError,
    TransportError,
    WebhookSecretMissingError,
)
from .models import ChannelConfig, ChannelMessage, ChannelType, MessageLevel
from .null_channel import NullChannel, RecordedSend
from .registry import (
    ChannelAdapterRegistry,
    WebhookChannelAdapter,
    build_default_registry,
)
from .results import (
    ChannelHealth,
    ChannelSendResult,
    CircuitState,
    ErrorCategory,
    SendStatus,
    ValidationResult,
)
from .retry import DEFAULT_RETRY_POLICY, RetryPolicy
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    ChannelTransport,
    TransportResponse,
    UrllibChannelTransport,
    default_headers,
    encode_json_body,
    redact_webhook_url,
    validate_webhook_url,
)

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "DEFAULT_TIMEOUT_SECONDS",
    "BaseChannel",
    "CapabilityDescriptor",
    "CapabilityNotDeclaredError",
    "CardUpdateCapability",
    "ChannelAdapter",
    "ChannelAdapterRegistry",
    "ChannelCapability",
    "ChannelCapabilitySet",
    "ChannelConfig",
    "ChannelDisabledError",
    "ChannelError",
    "ChannelHealth",
    "ChannelManager",
    "ChannelMessage",
    "ChannelNotFoundError",
    "ChannelSendResult",
    "ChannelTransport",
    "ChannelType",
    "CircuitState",
    "ErrorCategory",
    "InboundActivityContext",
    "InvalidWebhookURLError",
    "MessageLevel",
    "NullChannel",
    "OutboundCapability",
    "ProcessingOutcome",
    "ProcessingStatusCapability",
    "RecordedSend",
    "RetryPolicy",
    "SendStatus",
    "TransportError",
    "TransportResponse",
    "UrllibChannelTransport",
    "ValidationResult",
    "WebhookChannelAdapter",
    "WebhookSecretMissingError",
    "build_default_registry",
    "default_headers",
    "encode_json_body",
    "redact_webhook_url",
    "validate_webhook_url",
]
