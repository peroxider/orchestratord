"""Single-agent mode — pass-through wrapper over the existing AgentRunner.

This is Phase-1's only registered mode and the safe fallback for every
later phase. Behavior must be byte-identical to calling
``self.agent_runner.run(session, workflow, ...)`` directly so the 270+
existing orchestrator tests keep passing without modification.

There is intentionally **no** branching here: ``run`` simply delegates.
If you find yourself adding logic to this class, you probably want a
new mode (Pipeline / Coordinator / Debate) instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..backend_runner import BackendRunner as AgentRunner
    from ..session_state import AgentSession
    from ..config.schema import WorkflowConfig


class SingleModeRunner:
    """Wraps an ``AgentRunner`` to satisfy the ``ModeRunner`` Protocol."""

    def __init__(self, agent_runner: "AgentRunner") -> None:
        self._agent_runner = agent_runner

    async def run(
        self,
        session: "AgentSession",
        workflow: "WorkflowConfig",
        **hooks: Any,
    ) -> Any:
        return await self._agent_runner.run(session, workflow, **hooks)


__all__ = ["SingleModeRunner"]
