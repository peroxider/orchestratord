"""Tool-call audit bypass tests.

Covers the per-tool NDJSON bypass and the report_writer field
that registers the audit path on the run report.  Mirrors the
pattern of tests/test_orchestrator_trackers.py (TestReportWriter):
TemporaryDirectory + HOME override + ReportResult / NDJSON assertions.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from orchestratord.adapters.clawcodex import (
    PhaseComplete,
    SessionComplete,
    ToolCallEvent,
    ToolResultEvent,
)
from orchestratord.agent_runner import AgentRunner, AgentSession
from orchestratord.config.schema import (
    AgentConfig,
    SandboxConfig,
    WorkflowConfig,
)