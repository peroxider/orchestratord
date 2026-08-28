"""Unit tests for the Feishu activity sink (Activity visibility).

Processing reactions are owned by the IM gateway. This sink translates
orchestrator lifecycle events into progress updates to a placeholder card.

These tests exercise the translation layer in isolation, mocking both
the :class:`FeishuAppChannelAdapter` and the asynchronous coroutines
the sink schedules.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from orchestratord.events.agent_events import PhaseComplete, SessionComplete, TurnComplete
from orchestratord.feishu_activity_sink import (
    FeishuActivitySink,
    drain_pending_for_test,
)
from orchestratord.status_dashboard import SessionStatus
from dataclasses import dataclass
from enum import Enum


class ChannelCapability(Enum):
    CARD_UPDATE = "card_update"


class ChannelCapabilitySet:
    def __init__(self, capabilities: set[ChannelCapability]) -> None:
        self._capabilities = capabilities

    @classmethod
    def of(cls, *capabilities: ChannelCapability) -> "ChannelCapabilitySet":
        return cls(set(capabilities))

    def __contains__(self, item: ChannelCapability) -> bool:
        return item in self._capabilities


@dataclass
class InboundActivityContext:
    message_id: str
    chat_id: str


# ---------------------------------------------------------------------------
# Fake adapter / dashboard helpers
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Drop-in replacement for :class:`FeishuAppChannelAdapter` for tests.

    Records every call so the assertions can introspect the order and
    arguments; returns ``None`` / ``True`` for the success path.
    """