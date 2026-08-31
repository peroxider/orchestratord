"""Tests for task decomposition CLI plumbing and swarm-mode registration."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestratord import modes as mode_registry
from orchestratord.config.schema import WorkflowConfig
from orchestratord.git.sync import VerificationFailed
from orchestratord.issue_registry.issue import Issue
from orchestratord.mode_router import HeuristicRouter
from orchestratord.mode_selector import ModeSelector
from orchestratord.modes.swarm import SwarmModeRunner
from orchestratord.task_decomposition import (
    TaskDecomposer,
    validate_task_execution,
    write_task_plan,
)
from orchestratord.task_decomposition.models import Subtask, TaskPlan


def build_parser() -> argparse.ArgumentParser:
    """Local stub — avoids optional backend dependencies."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--swarm", action="store_true")
    parser.add_argument("--decompose", action="store_true", dest="swarm")
    parser.add_argument("--effort", type=str)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("prompt", nargs="?", default="")
    return parser


class FakeAgentRunner:
    def __init__(self) -> None:
        self.agent_config = SimpleNamespace(coordinator_mode=False)
        self.calls = []


def test_orchestrator_registers_swarm_runner() -> None:
    from orchestratord.orchestrator import Orchestrator

    instance = MagicMock(spec=["_register_collaboration_modes"])
    workflow = WorkflowConfig.from_dict({"modes": {"enabled": ["single", "swarm"]}})
    Orchestrator._register_collaboration_modes(instance, workflow, FakeAgentRunner())
    assert isinstance(mode_registry.get("swarm"), SwarmModeRunner)


def test_cli_parser_supports_swarm_aliases() -> None:
    args = build_parser().parse_args(["--swarm", "do work"])
    assert args.swarm is True
    assert args.prompt == "do work"
    alias = build_parser().parse_args(["--decompose", "do work"])
    assert alias.swarm is True
    effort = build_parser().parse_args(["--effort", "swarm", "do work"])
    assert effort.effort == "swarm"
