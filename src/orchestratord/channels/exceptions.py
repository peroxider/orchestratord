"""Channels domain exceptions."""


class ChannelError(RuntimeError):
    """Base error for channel operations."""


class InvalidWebhookURLError(ChannelError):
    """Raised when a webhook URL is malformed, has the wrong scheme, or
    points at a private network address when public-only is enforced."""


class WebhookSecretMissingError(ChannelError):
    """Raised when a channel requires a signing secret (Feishu, WeChat) but
    none was configured."""


class TransportError(ChannelError):
    """Raised when the underlying HTTP transport fails."""


class ChannelNotFoundError(ChannelError):
    """Raised when sending to a channel name that is not registered."""


class ChannelDisabledError(ChannelError):
    """Raised when sending to a channel that is registered but disabled."""
