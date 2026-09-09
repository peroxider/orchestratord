"""In-process command service shared by terminal and gateway adapters."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import issue, server
from .models import CommandContext, CommandOutput, CommandRequest, CommandResult

ISSUE_ACTIONS = frozenset(
    {
        "list",
        "show",
        "stop",
        "pause",
        "resume",
        "clarify",
        "inject",
        "workspace",
        "review",
        "retry",
        "rebase",
        "feedback",
    }
)


class OrchestratorCommandService:
    """Serialize commands on the daemon loop with explicit workspace ownership.

    Network waits are cancellable coroutines. No worker thread, child CLI,
    argv rewrite or global output redirection is used to execute commands.
    A timeout cancels remaining work; it does not roll back completed effects.
    """

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        workflow_path: str | Path | None = None,
        runtime_supplier: Callable[[], Any] | None = None,
        timeout_seconds: float | None = 60.0,
        confirm: Callable[[str], str] | None = None,
        output_limit: int | None = 64_000,
        output_factory: Callable[[], CommandOutput] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.workflow_path = Path(workflow_path).resolve() if workflow_path else None
        self.runtime_supplier = runtime_supplier
        self.timeout_seconds = (
            max(0.01, timeout_seconds) if timeout_seconds is not None else None
        )
        self.confirm = confirm
        self.output_limit = output_limit
        self.output_factory = output_factory
        self._lock = asyncio.Lock()

    async def execute(self, request: CommandRequest) -> CommandResult:
        context = CommandContext(
            workspace_root=self.workspace_root,
            workflow_path=self.workflow_path,
            output=self.output_factory()
            if self.output_factory
            else CommandOutput(limit=self.output_limit),
        )
        if self.confirm is not None:
            context.confirm = self.confirm
        try:
            # Queue time is part of the budget; expired waiters never execute.
            async with asyncio.timeout(self.timeout_seconds):
                async with self._lock:
                    if self.runtime_supplier is not None:
                        context.runtime = self.runtime_supplier()
                        if context.runtime is None:
                            return CommandResult(
                                1, stderr="error: orchestrator is not ready"
                            )
                    code = await self._execute(context, request)
        except TimeoutError:
            code = 124
            context.output.write(
                "error: command timed out; completed effects are not rolled back",
                error=True,
            )
        except (OSError, ValueError) as exc:
            code = 1
            context.output.write(f"error: {exc}", error=True)
        return CommandResult(code, context.output.stdout, context.output.stderr)

    async def _execute(self, context: CommandContext, request: CommandRequest) -> int:
        from orchestratord.workspace_locator import (
            get_registry_path,
            get_workspace_root,
        )

        args = request.namespace()
        if self.workspace_root is not None:
            requested = getattr(args, "workspace", None)
            if requested and Path(requested).resolve() != self.workspace_root:
                context.output.write(
                    "error: command targets a different orchestrator workspace",
                    error=True,
                )
                return 2
            workflow = getattr(args, "workflow", None)
            if workflow and (
                self.workflow_path is None
                or Path(workflow).resolve() != self.workflow_path
            ):
                context.output.write(
                    "error: command targets a different orchestrator workflow",
                    error=True,
                )
                return 2
            args.workspace = str(self.workspace_root)
            args.workflow = str(self.workflow_path) if self.workflow_path else None
        ws = get_workspace_root(
            workspace_arg=getattr(args, "workspace", None),
            workflow_path=getattr(args, "workflow", None),
        )
        context.workspace_root = ws
        if request.resource == "server" and request.action == "status":
            return server._run_status(context, args)
        if request.resource != "issue" or request.action not in ISSUE_ACTIONS:
            context.output.write("error: unsupported application command", error=True)
            return 2
        registry_path = get_registry_path(workspace_arg=ws)
        action = request.action
        operation = getattr(issue, f"_run_{action}")
        if action in {"list", "show"}:
            result = operation(context, registry_path, args)
        elif action in {"review", "retry", "rebase", "feedback"}:
            result = operation(context, registry_path, args, workspace_root=ws)
        elif action in {"stop", "clarify"}:
            result = operation(
                context, args, registry_path=registry_path, workspace_root=ws
            )
        elif action in {"pause", "resume"}:
            result = operation(context, args, workspace_root=ws)
        else:
            result = operation(context, args)
        return await result if inspect.isawaitable(result) else result
