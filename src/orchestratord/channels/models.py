"""Channels data models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ChannelType(str, Enum):
    FEISHU = "feishu"
    SLACK = "slack"
    DISCORD = "discord"
    WECHAT = "wechat"
    MCP_PUSH = "mcp_push"


class MessageLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    SUCCESS = "success"


@dataclass
class ChannelMessage:
    text: str
    level: MessageLevel = MessageLevel.INFO
    title: str | None = None
    markdown: bool = True
    attachments: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("ChannelMessage.text must be a string")
        if not self.text:
            raise ValueError("ChannelMessage.text must be non-empty")
        if len(self.text) > 30_000:
            raise ValueError("ChannelMessage.text exceeds 30000 character safety cap")
        if self.title is not None and not isinstance(self.title, str):
            raise TypeError("ChannelMessage.title must be a string or None")
        if self.title is not None and len(self.title) > 200:
            raise ValueError("ChannelMessage.title exceeds 200 character safety cap")
        if self.attachments is not None and not isinstance(self.attachments, list):
            raise TypeError("ChannelMessage.attachments must be a list or None")
        if self.metadata is not None and not isinstance(self.metadata, dict):
            raise TypeError("ChannelMessage.metadata must be a dict or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "level": self.level.value,
            "title": self.title,
            "markdown": self.markdown,
            "attachments": list(self.attachments) if self.attachments is not None else None,
            "metadata": dict(self.metadata) if self.metadata is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelMessage:
        if not isinstance(data, dict):
            raise TypeError("ChannelMessage data must be a dict")
        level = data.get("level", MessageLevel.INFO.value)
        if not isinstance(level, str):
            raise TypeError("ChannelMessage.level must be a string")
        return cls(
            text=str(data["text"]),
            level=MessageLevel(level),
            title=data.get("title"),
            markdown=bool(data.get("markdown", True)),
            attachments=data.get("attachments"),
            metadata=data.get("metadata"),
        )


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class ChannelConfig:
    type: ChannelType
    webhook_url: str = ""
    name: str = ""
    enabled: bool = True
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, ChannelType):
            raise TypeError("ChannelConfig.type must be a ChannelType")
        if not isinstance(self.webhook_url, str):
            raise TypeError("ChannelConfig.webhook_url must be a string")
        if not self.webhook_url:
            mode = str((self.extra or {}).get("connection_mode") or "").lower()
            if not (self.type is ChannelType.FEISHU and mode):
                raise ValueError("ChannelConfig.webhook_url must be a non-empty string")
        if not _NAME_RE.match(self.name or ""):
            raise ValueError(
                "ChannelConfig.name must match [A-Za-z0-9._-]{1,64}; got: " + repr(self.name)
            )
        if self.extra is not None and not isinstance(self.extra, dict):
            raise TypeError("ChannelConfig.extra must be a dict or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "webhook_url": self.webhook_url,
            "name": self.name,
            "enabled": self.enabled,
            "extra": dict(self.extra) if self.extra is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelConfig:
        if not isinstance(data, dict):
            raise TypeError("ChannelConfig data must be a dict")
        return cls(
            type=ChannelType(str(data["type"])),
            webhook_url=str(data.get("webhook_url") or ""),
            name=str(data["name"]),
            enabled=bool(data.get("enabled", True)),
            extra=data.get("extra"),
        )


__all__ = [
    "ChannelConfig",
    "ChannelMessage",
    "ChannelType",
    "MessageLevel",
]
