"""Native OrchestrationSubsystem for orchestratord.

Wires together WorkspaceManager, TrackerAdapter, AgentRunner/BackendRunner,
and Orchestrator. This is the top-level
entry point for the autonomous orchestration engine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .applications.issue_pr.lifecycle import IssueToPrLifecycle
    from .applications.issue_pr.provider import IssuePrWorkProvider
    from .backend_runner import BackendRunner
    from .kernel.events import KernelHooks
    from .orchestrator import Orchestrator
    from .spi.backend import AgentBackend

# 组合根装配：业务 prompt 模板注册进 kernel PromptRouter（DESIGN §4.5/P2）。
import orchestratord.applications.issue_pr.prompts  # noqa: F401

from .config.schema import WorkflowConfig
from .status_dashboard import StatusDashboard
from .tracker import TrackerAdapter
from .tracker_kinds import (
    create_tracker_adapter,
    repository_clone_url_for_tracker,
)
from .workspace import WorkspaceConfig, WorkspaceManager

logger = logging.getLogger(__name__)


@dataclass
class OrchestrationSubsystem:
    """Autonomous mode orchestration engine.
    """

    workflow: WorkflowConfig
    workspace_manager: WorkspaceManager
    tracker_adapter: TrackerAdapter
    agent_runner: BackendRunner
    status_dashboard: StatusDashboard
    stage_runners: dict[str, BackendRunner]
    _orchestrator: Orchestrator | None = None
    _workflow_yaml_path: str | None = None
    _bundle_dir: Path | None = None
    _backend: "AgentBackend | None" = None
    kernel_hooks: KernelHooks | None = None
    # P4 C2c（DESIGN §4.2/G2）：业务注入点——Application / WorkProvider
    # 协议对象。``None``（默认）时 run() 惰性装配 issue→PR 默认业务；
    # 注入实例以无宿主形态构造，由 Orchestrator.__init__ 回绑 ``_host``。
    application: IssueToPrLifecycle | None = None
    work_provider: IssuePrWorkProvider | None = None

    def __init__(
        self,
        workflow_config: WorkflowConfig,
        *,
        workflow_yaml_path: str | None = None,
        backend: "AgentBackend | None" = None,
        clarifier_provider_factory: "Callable[[], Any] | None" = None,
        kernel_hooks: KernelHooks | None = None,
        application: IssueToPrLifecycle | None = None,
        work_provider: IssuePrWorkProvider | None = None,
    ) -> None:
        from .backend_runner import BackendRunner

        self.workflow = workflow_config
        resolved_yaml = workflow_yaml_path

        self._workflow_yaml_path = resolved_yaml
        self._bundle_dir = None
        if resolved_yaml:
            self._bundle_dir = Path(resolved_yaml).resolve().parent

        self.workspace_manager = WorkspaceManager(
            WorkspaceConfig(
                root=Path(workflow_config.workspace.root),
                hooks=workflow_config.workspace.hooks,
                repo_clone_url=workflow_config.workspace.repo_clone_url
                or repository_clone_url_for_tracker(workflow_config.tracker),
                upstream_clone_url=workflow_config.workspace.upstream_clone_url,
                clone_depth=workflow_config.workspace.clone_depth,
                checkout_issue_branch=(workflow_config.workspace.checkout_issue_branch),
                git_username=workflow_config.workspace.git_username,
                git_email=workflow_config.workspace.git_email,
                git_token=workflow_config.workspace.git_token,
                gitignore_patterns=workflow_config.workspace.gitignore_patterns,
                strategy=workflow_config.workspace.strategy,
                base_branch=workflow_config.workspace.base_branch,
                integration_branch=workflow_config.workspace.integration_branch,
                require_clean_start=workflow_config.workspace.require_clean_start,
                require_clean_between_issues=(
                    workflow_config.workspace.require_clean_between_issues
                ),
                preserve_on_terminal=workflow_config.workspace.preserve_on_terminal,
                sequential_lock=workflow_config.workspace.sequential_lock,
            )
        )
        self.tracker_adapter = create_tracker_adapter(workflow_config.tracker)
        if backend is None:
            raise ValueError(
                "An AgentBackend must be supplied explicitly. "
                "Install/select a backend through the backend registry; "
                "orchestratord does not provide an implicit backend."
            )
        self._backend = backend
        self.agent_runner = BackendRunner(
            backend=backend,
            agent_config=workflow_config.agent,
            sandbox_config=workflow_config.sandbox,
            workspace_cfg=workflow_config.workspace,
        )

        # Build per-stage AgentRunners for multi-model stage overrides.
        self.stage_runners = {}
        for stage_name, override in workflow_config.agent.stage_overrides.items():
            from dataclasses import replace

            stage_config = replace(
                workflow_config.agent,
                provider=override.get("provider", workflow_config.agent.provider),
                model=override.get("model", workflow_config.agent.model),
            )
            self.stage_runners[stage_name] = BackendRunner(
                backend=backend,
                agent_config=stage_config,
                sandbox_config=workflow_config.sandbox,
                workspace_cfg=workflow_config.workspace,
            )
            logger.info(
                "stage runner [%s]: provider=%s model=%s",
                stage_name,
                stage_config.provider,
                stage_config.model,
            )

        self.status_dashboard = StatusDashboard()
        self._orchestrator = None
        self._clarifier_provider_factory = clarifier_provider_factory
        # KernelHooks（DESIGN §4.7）：宿主注入的装配钩子，run() 透传 Kernel。
        self.kernel_hooks = kernel_hooks
        # P4 C2c（DESIGN §4.2/G2）：业务注入点，``None`` → run() 惰性装配
        # issue→PR 默认业务（PEP 562 时序，见 run() 内说明）。
        self.application = application
        self.work_provider = work_provider


    async def run(self) -> None:
        """Start polling and issue execution. Runs until cancelled."""
        from .orchestrator import Orchestrator

        # C2c 装配（DESIGN §4.2/G2）：Application / WorkProvider 由组合根
        # 在此惰性供给——注入优先（G2「组合根可替换」），默认业务
        # （issue→PR）此刻才 import。本模块顶层不得触达 applications 的
        # app/lifecycle（import 时序契约，见
        # applications/issue_pr/__init__.py）；实例以无宿主形态构造，
        # 由 Orchestrator.__init__ 回绑 ``_host``（两段式装配）。
        if self.application is not None:
            application = self.application
        else:
            from .applications.issue_pr.lifecycle import IssueToPrLifecycle

            application = IssueToPrLifecycle()
        if self.work_provider is not None:
            work_provider = self.work_provider
        else:
            from .applications.issue_pr.provider import IssuePrWorkProvider

            work_provider = IssuePrWorkProvider()

        self._orchestrator = Orchestrator(
            workflow=self.workflow,
            tracker=self.tracker_adapter,
            workspace=self.workspace_manager,
            agent_runner=self.agent_runner,
            backend=self._backend,
            status_dashboard=self.status_dashboard,
            stage_runners=self.stage_runners,
            workflow_yaml_path=self._workflow_yaml_path,
            clarifier_provider_factory=self._clarifier_provider_factory,
            kernel_hooks=self.kernel_hooks,
            application=application,
            work_provider=work_provider,
        )
        await self._orchestrator.run()

    async def shutdown(self) -> None:
        """Graceful shutdown — stop polling, clean up workspaces."""
        if self._orchestrator:
            await self._orchestrator.shutdown()
