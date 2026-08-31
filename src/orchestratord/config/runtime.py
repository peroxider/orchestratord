"""Configuration views separating runtime concerns from business policy."""

from __future__ import annotations

from dataclasses import dataclass

from .schema import (
    AgentConfig,
    ObservabilityConfig,
    PollingConfig,
    PrConflictScanConfig,
    PrTemplateConfig,
    ReviewFeedbackConfig,
    SandboxConfig,
    ServerConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkerConfig,
    WorkspaceConfig,
)


@dataclass(frozen=True)
class RuntimeConfig:
    """Backend execution and control-plane configuration."""

    agent: AgentConfig
    sandbox: SandboxConfig
    worker: WorkerConfig
    observability: ObservabilityConfig
    server: ServerConfig

    @classmethod
    def from_legacy(cls, config: WorkflowConfig) -> "RuntimeConfig":
        return cls(config.agent, config.sandbox, config.worker, config.observability, config.server)


@dataclass(frozen=True)
class IssuePrConfig:
    """Issue-to-PR application policy kept outside the orchestration core."""

    tracker: TrackerConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    review_feedback: ReviewFeedbackConfig
    pr_template: PrTemplateConfig
    pr_conflict_scan: PrConflictScanConfig

    @classmethod
    def from_legacy(cls, config: WorkflowConfig) -> "IssuePrConfig":
        return cls(
            config.tracker,
            config.polling,
            config.workspace,
            config.review_feedback,
            config.pr_template,
            config.pr_conflict_scan,
        )
