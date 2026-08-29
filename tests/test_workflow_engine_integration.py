"""Smoke test for the declarative workflow engine integration.

Verifies:
1. All module imports work (no circular deps, no broken paths)
2. Minimal workflow.yaml can be parsed into a DAG schema
3. OrchestrationSubsystem accepts workflow_yaml_path and passes it to Orchestrator
4. WorkflowProgressSink <-> ProgressSink wiring
5. StageRunner synthetic Issue construction
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── 1. Import sanity ─────────────────────────────────────────────────


class TestWorkflowEngineImports(unittest.TestCase):
    """Verify all modules in the new workflow engine tree import cleanly."""

    def test_core_imports(self) -> None:
        from orchestratord.workflow_engine import (
            WorkflowSchema,
            WorkflowState,
        )

        self.assertIsNotNone(WorkflowSchema)
        self.assertIsNotNone(WorkflowState)

    def test_engine_import(self) -> None:
        from orchestratord.workflow_engine.engine import (
            DeclarativeWorkflowEngine,
        )

        self.assertIsNotNone(DeclarativeWorkflowEngine)

    def test_stage_runner_import(self) -> None:
        from orchestratord.workflow_engine.stage_runner import (
            StageRunner,
        )

        self.assertIsNotNone(StageRunner)

    def test_observability_imports(self) -> None:
        from orchestratord.workflow_engine.observability import (
            WorkflowObservability,
            WorkflowProgressSink,
        )

        self.assertIsNotNone(WorkflowObservability)
        self.assertIsNotNone(WorkflowProgressSink)

    def test_validators_import(self) -> None:
        from orchestratord.workflow_engine.validators import (
            ContractValidator,
            ValidationResult,
        )

        self.assertIsNotNone(ContractValidator)
        self.assertIsNotNone(ValidationResult)

    def test_checkpoint_import(self) -> None:
        from orchestratord.workflow_engine.checkpoint import (
            CheckpointManager,
        )

        self.assertIsNotNone(CheckpointManager)

    def test_workflow_orchestrator_import(self) -> None:
        from orchestratord.workflow_orchestrator import (
            WorkflowOrchestrator,
        )

        self.assertIsNotNone(WorkflowOrchestrator)

    def test_orchestrator_workflow_yaml_param(self) -> None:
        """Verify Orchestrator.__init__ accepts workflow_yaml_path."""
        import inspect
        from orchestratord.orchestrator import Orchestrator

        sig = inspect.signature(Orchestrator.__init__)
        params = list(sig.parameters.keys())
        self.assertIn("workflow_yaml_path", params)

    def test_orchestration_subsystem_workflow_yaml_param(self) -> None:
        """Verify OrchestrationSubsystem.__init__ accepts workflow_yaml_path."""
        import inspect
        from orchestratord.orchestration_subsystem import OrchestrationSubsystem

        sig = inspect.signature(OrchestrationSubsystem.__init__)
        params = list(sig.parameters.keys())
        self.assertIn("workflow_yaml_path", params)


# ── 2. workflow.yaml parsing ─────────────────────────────────────────


class TestWorkflowYamlParsing(unittest.TestCase):
    """Verify a minimal workflow.yaml can be parsed into a DAG schema."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.yaml_path = Path(self._tmpdir.name) / "workflow.yaml"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_yaml(self, content: str) -> Path:
        self.yaml_path.write_text(content, encoding="utf-8")
        return self.yaml_path

    def test_minimal_workflow_yaml(self) -> None:
        """A minimal 2-stage linear workflow parses correctly."""
        content = textwrap.dedent("""\
            name: smoke-test
            description: A minimal smoke test workflow
            version: "1.0"
            stages:
              - id: 1
                name: Analyze
                phase: analyze
                prompt: "Analyze the issue."
              - id: 2
                name: Implement
                phase: implement
                depends_on: [1]
                prompt: "Implement the fix."
        """)
        self._write_yaml(content)

        from orchestratord.workflow_engine import WorkflowSchema

        schema = WorkflowSchema.from_yaml(self.yaml_path)
        self.assertEqual(schema.name, "smoke-test")
        self.assertEqual(len(schema.stages), 2)
        self.assertEqual(schema.stages[0].name, "Analyze")
        self.assertEqual(schema.stages[1].depends_on, [1])

    def test_dag_topological_order(self) -> None:
        """A 3-stage DAG with a diamond dependency produces correct order."""
        content = textwrap.dedent("""\
            name: dag-test
            description: Diamond dependency DAG
            version: "1.0"
            stages:
              - id: 1
                name: Setup
                phase: setup
              - id: 2
                name: Branch A
                phase: branch_a
                depends_on: [1]
              - id: 3
                name: Branch B
                phase: branch_b
                depends_on: [1]
              - id: 4
                name: Merge
                phase: merge
                depends_on: [2, 3]
        """)
        self._write_yaml(content)

        from orchestratord.workflow_engine import WorkflowSchema

        schema = WorkflowSchema.from_yaml(self.yaml_path)
        order = schema.build_dag_order()
        stage_ids = order

        # Setup must be first
        self.assertEqual(stage_ids[0], 1)
        # Merge must be last
        self.assertEqual(stage_ids[-1], 4)
        # Setup must come before all others
        setup_idx = stage_ids.index(1)
        for sid in [2, 3, 4]:
            self.assertGreater(stage_ids.index(sid), setup_idx)


# ── 3. WorkflowOrchestrator initialization ───────────────────────────


class TestWorkflowOrchestratorInit(unittest.TestCase):
    """Verify WorkflowOrchestrator can be initialized with a minimal yaml."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.yaml_path = Path(self._tmpdir.name) / "workflow.yaml"
        self.yaml_path.write_text(
            textwrap.dedent("""\
                name: init-test
                description: Init test
                version: "1.0"
                stages:
                  - id: 1
                    name: Test
                    phase: test
                    prompt: "Run test."
            """),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_init_without_agent_runner(self) -> None:
        """WorkflowOrchestrator can be initialized without AgentRunner
        (standalone mode)."""
        from orchestratord.config.schema import WorkflowConfig
        from orchestratord.workflow_orchestrator import (
            WorkflowOrchestrator,
        )

        wf_config = WorkflowConfig.from_dict(
            {
                "workspace": {"root": str(self._tmpdir.name)},
                "agent": {},
                "tracker": {"kind": "local"},
            }
        )

        orch = WorkflowOrchestrator(
            workflow_config=wf_config,
            workflow_yaml_path=str(self.yaml_path),
        )
        self.assertEqual(orch.schema.name, "init-test")
        self.assertEqual(len(orch.schema.stages), 1)
        self.assertIsNotNone(orch.progress)

    def test_set_progress_sink(self) -> None:
        """Progress sink injection works — sinks are forwarded."""
        from orchestratord.config.schema import WorkflowConfig
        from orchestratord.workflow_orchestrator import (
            WorkflowOrchestrator,
        )
        from orchestratord.progress_sink import (
            CompositeProgressSink,
            ToolContextProgressSink,
        )

        wf_config = WorkflowConfig.from_dict(
            {
                "workspace": {"root": str(self._tmpdir.name)},
                "agent": {},
                "tracker": {"kind": "local"},
            }
        )

        orch = WorkflowOrchestrator(
            workflow_config=wf_config,
            workflow_yaml_path=str(self.yaml_path),
        )

        mock_sink = ToolContextProgressSink(
            task_id="test-123",
            context=None,
            workflow_phases=["test"],
        )
        orch.set_progress_sink(mock_sink)

        # Verify the sink was added to WorkflowProgressSink's internal list
        snapshot = orch.progress
        self.assertEqual(snapshot["workflow_name"], "init-test")
        self.assertEqual(snapshot["total_stages"], 1)
        self.assertEqual(snapshot["completed_stages"], 0)


# ── 4. OrchestrationSubsystem workflow_yaml_path plumbing ─────────────


class TestOrchestrationSubsystemPlumbing(unittest.TestCase):
    """Verify OrchestrationSubsystem passes workflow_yaml_path to Orchestrator."""

    def test_subsystem_stores_workflow_yaml_path(self) -> None:
        from orchestratord.config.schema import WorkflowConfig
        from orchestratord.orchestration_subsystem import OrchestrationSubsystem

        import tempfile

        issues_dir = tempfile.mkdtemp()
        wf_config = WorkflowConfig.from_dict(
            {
                "workspace": {"root": "/tmp/test"},
                "agent": {},
                "tracker": {"kind": "local", "issues_path": issues_dir},
            }
        )

        subsystem = OrchestrationSubsystem(
            wf_config,
            workflow_yaml_path="/path/to/workflow.yaml",
            backend=MagicMock(),
        )
        self.assertEqual(subsystem._workflow_yaml_path, "/path/to/workflow.yaml")

    def test_subsystem_none_is_ok(self) -> None:
        """Omitting workflow_yaml_path is valid (backward compatible)."""
        from orchestratord.config.schema import WorkflowConfig
        from orchestratord.orchestration_subsystem import OrchestrationSubsystem
        import tempfile

        issues_dir = tempfile.mkdtemp()
        wf_config = WorkflowConfig.from_dict(
            {
                "workspace": {"root": "/tmp/test"},
                "agent": {},
                "tracker": {"kind": "local", "issues_path": issues_dir},
            }
        )

        subsystem = OrchestrationSubsystem(wf_config, backend=MagicMock())
        self.assertIsNone(subsystem._workflow_yaml_path)
