"""Slack channel implementation (incoming webhooks)."""

from __future__ import annotations

import json

from .base import BaseChannel
from .models import ChannelMessage
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    TransportResponse,
    default_headers,
    encode_json_body,
)


class SlackChannel(BaseChannel):
    def format_message(self, message: ChannelMessage) -> tuple[bytes, dict[str, str]]:
        if message.markdown and message.title:
            payload: dict[str, object] = {
                "text": message.title,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{message.title}*\n{message.text}",
                        },
                    }
                ],
            }
        else:
            payload = {"text": message.text}
        return encode_json_body(payload), default_headers()

    async def send(self, message: ChannelMessage) -> bool:
        body, headers = self.format_message(message)
        response: TransportResponse = await self._transport.post(
            self._config.webhook_url,
            body,
            headers=headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if response.body:
            try:
                decoded = json.loads(response.body.decode("utf-8"))
                # An ``ok`` field is authoritative when present.
                if isinstance(decoded, dict) and "ok" in decoded:
                    return bool(decoded.get("ok"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return 200 <= response.status < 300


__all__ = ["SlackChannel"]
