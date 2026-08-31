"""Issue Clarifier — manual E2E with real provider + LocalTracker.

Requires:
  ORCHESTRATORD_TEST_PROVIDER=openai (or any configured provider name)
  ORCHESTRATORD_TEST_MODEL=gpt-4o-mini   (optional, default: gpt-4o-mini)

Run locally:
  ORCHESTRATORD_TEST_PROVIDER=openai python3 -m pytest \\
    tests/orchestrator/manual_e2e_f124.py -v -s

CI skips this file via --ignore and the @skipif decorator below.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Generator

import pytest

from orchestratord.issue_clarifier.resolver import ClarificationConfig, ClarificationResolver
from orchestratord.issue_clarifier.queue import ClarificationQueue, ClarificationStatus
from orchestratord.config.schema import ClarifierConfig
from orchestratord.issue_registry.issue import Issue
from orchestratord.issue_clarifier import ClarifierCache, IssueClarifierService
from orchestratord.issue_clarifier.gate import IssueClarificationGate
from orchestratord.issue_registry import IssueRegistry
from orchestratord.local_tracker.adapter import LocalTrackerAdapter


# Tests in this file intentionally talk to a real provider / network.
# The conftest PATH guard would otherwise block every backend CLI.
pytestmark = pytest.mark.uses_real_cli


@pytest.mark.skipif(
    not os.environ.get("ORCHESTRATORD_TEST_PROVIDER"),
    reason="set ORCHESTRATORD_TEST_PROVIDER to run real-provider E2E tests",
)
class TestLongRunningE2E:
    """Real-provider E2E tests for the issue clarifier pipeline.

    These tests exercise the full endpoint-to-endpoint flow:
      issue → analyze → block → answer → unblock → dispatch
    with a real LLM provider and a LocalTrackerAdapter.
    """

    @pytest.fixture
    def setup(self, tmp_path: Path) -> Generator:
        """Build a minimal E2E environment: LocalTracker + real provider + gate."""
        tracker_path = tmp_path / "tracker"
