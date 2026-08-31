"""AgentTaskRunner — protocol for executing an AgentTask.

Both historical runner names and ``BackendRunner`` (SPI path)
implement this protocol.  The Orchestrator depends on the protocol,
not on either concrete implementation.

See ``DESIGN_agent_task_abstraction.md`` for the full architecture.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from .task import AgentTask, AgentTaskResult, ProgressEvent


class AgentTaskRunner(Protocol):
    """Protocol for executing an AgentTask.

    Both historical runner names and ``BackendRunner`` (SPI path)
    implement this protocol.  The Orchestrator depends on the protocol,
    not on either concrete implementation.
    """

    async def run_task(
        self,
        task: AgentTask,
        *,
        progress_callback: Callable[[ProgressEvent], Awaitable[None]] | None = None,
        diagnostics_callback: Callable[[AgentTaskResult], None] | None = None,
    ) -> AgentTaskResult:
        """Execute *task* and return its result.

        Args:
            task: The task to execute.
            progress_callback: If set, called for each event during
                execution (text deltas, tool calls, turn completes, etc.).
                Layer 1 does not know or care who consumes these.
            diagnostics_callback: If set, called once with the final
                result before returning.

        Returns:
            AgentTaskResult with the terminal status and outputs.
        """
        ...
