from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestratord.config.schema import WorkflowConfig
from orchestratord.orchestration_subsystem import OrchestrationSubsystem
from orchestratord.cli.issue import _run_diff
from orchestratord.issue_registry import IssueRegistry
from orchestratord.orchestrator import Orchestrator
from orchestratord.workspace import WorkspaceConfig, WorkspaceManager


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)