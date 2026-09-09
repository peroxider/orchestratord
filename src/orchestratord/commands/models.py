"""Request-local dependencies and results for command application services."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from orchestratord.paths import AUDIT_LOG, ORCHESTRATOR_DIR

if TYPE_CHECKING:
    from orchestratord.issue_clarifier.queue import ClarificationQueue
    from orchestratord.issue_registry import IssueRegistry


@dataclass(frozen=True)
class CommandRequest:
    resource: str
    action: str
    options: dict[str, Any] = field(default_factory=dict)

    def namespace(self) -> argparse.Namespace:
        return argparse.Namespace(**self.options)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class CommandService(Protocol):
    async def execute(self, request: CommandRequest) -> CommandResult: ...


@dataclass
class CommandOutput:
    """A bounded, request-owned output sink; never redirects process stdio."""

    limit: int | None = 64_000
    stdout: str = ""
    stderr: str = ""

    def write(
        self, *values: object, sep: str = " ", end: str = "\n", error: bool = False
    ) -> None:
        text = sep.join(str(value) for value in values) + end
        name = "stderr" if error else "stdout"
        current = getattr(self, name)
        if self.limit is not None:
            remaining = self.limit - len(current)
            if len(text) > remaining:
                marker = "\n[output truncated]\n"
                text = text[: max(0, remaining - len(marker))] + marker[:remaining]
        setattr(self, name, current + text)


def _no_confirmation(prompt: str) -> str:
    raise EOFError("Interactive confirmation is unavailable; use --yes")


@dataclass
class CommandContext:
    workspace_root: Path | None = None
    workflow_path: Path | None = None
    runtime: Any = None
    output: CommandOutput = field(default_factory=CommandOutput)
    confirm: Callable[[str], str] = _no_confirmation
    audit_path: Path | None = field(default_factory=lambda: AUDIT_LOG)
    metadata_directory: Path | None = field(default_factory=lambda: ORCHESTRATOR_DIR)

    def __post_init__(self) -> None:
        self.audit_path = self.audit_path or AUDIT_LOG
        self.metadata_directory = self.metadata_directory or ORCHESTRATOR_DIR

    def registry(self, path: Path) -> IssueRegistry:
        from orchestratord.issue_registry import IssueRegistry

        active = getattr(self.runtime, "_registry", None)
        if active is not None and Path(active._path).resolve() == Path(path).resolve():
            return active
        return IssueRegistry(path)

    def clarification_queue(self, path: Path | None) -> ClarificationQueue:
        from orchestratord.issue_clarifier.queue import ClarificationQueue

        active = getattr(self.runtime, "_clarification_queue", None)
        if (
            active is not None
            and self.workspace_root
            and path == self.workspace_root / ".orchestratord_clarification_queue.json"
        ):
            return active
        return ClarificationQueue(path)

    def multi_project_hint(self, projects: list[dict], command_hint: str) -> None:
        self.output.write(
            f"⚠  {len(projects)} running orchestrator projects detected.", error=True
        )
        self.output.write(
            f"   Command: {command_hint}\n\n   Running projects:", error=True
        )
        for project in projects:
            self.output.write(
                f"     [{project.get('project_slug', '?')}]  pid={project.get('pid', '?')}"
                f"  workspace={project.get('workspace_root', '?')}",
                error=True,
            )
        self.output.write(
            "\n   Use --workspace <path> or --workflow <path> to target a specific project.\n",
            error=True,
        )
