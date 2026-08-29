from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestratord.adapters.clawcodex import (
    SessionComplete,
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
)
from orchestratord.agent_runner import (
    AgentRunner,
    AgentSession,
    _megaturn_idle_stop_enabled,
)
from orchestratord.config.schema import AgentConfig, SandboxConfig, WorkflowConfig
from orchestratord.issue import Issue
from orchestratord.workspace import Workspace
class RateLimitError(Exception):
    """Local stub for rate-limit errors — avoids clawcodex_ext dependency."""

    def __init__(self, message: str = "", status: int = 429) -> None:
        super().__init__(message)
        self.status = status
from tests.spi_stub_helpers import spi_passthrough, spi_stub_backend
from tests.spi_stub_helpers import spi_passthrough, spi_stub_backend


def test_megaturn_idle_stop_is_disabled_for_swarm() -> None:
    assert not _megaturn_idle_stop_enabled(SimpleNamespace(run_kind="swarm"))
