"""Focused tests for the business-neutral dispatch kernel."""

from __future__ import annotations

import asyncio
import unittest

from orchestratord.agent.task import AgentTask, AgentTaskResult
from orchestratord.kernel.application import Outcome, PreparedRun
from orchestratord.kernel.events import KernelEventKind
from orchestratord.kernel.kernel import OrchestrationKernel
from orchestratord.kernel.run_context import RunContext
from orchestratord.kernel.work_provider import WorkItem


class _Provider:
    def __init__(self) -> None:
        self.items = [
            WorkItem(
                dedup_key="demo-1",
                task=AgentTask(id="demo-1", kind="demo", description="hello"),
            )
        ]
        self.rejected: list[str] = []

    async def poll(self):
        items, self.items = self.items, []
        return items

    async def on_dispatch_rejected(self, item, reason):
        self.rejected.append(reason)


class _Runner:
    async def run_task(self, task):
        return AgentTaskResult(
            task_id=task.id,
            kind=task.kind,
            status="completed",
            output_text="done",
            run_id="run-demo-1",
        )


class _Application:
    name = "demo"

    def __init__(self) -> None:
        self.events = []
        self.contexts: list[RunContext] = []

    async def prepare_run(self, item, ctx):
        self.contexts.append(ctx)
        return PreparedRun(prompt="prepared")

    async def interpret_result(self, item, result, ctx):
        return Outcome.dispose("done")

    def control_commands(self):
        return {}

    def prompt_profiles(self):
        return {}

    def on_kernel_event(self, event):
        self.events.append(event.kind)


class TestKernelDispatchLoop(unittest.TestCase):
    def test_generic_dispatch_consumes_outcome(self):
        async def scenario():
            provider = _Provider()
            application = _Application()
            kernel = OrchestrationKernel(
                application=application,
                work_provider=provider,
                runner=_Runner(),
                max_concurrent_agents=1,
            )
            accepted = await kernel.dispatch_once()
            await asyncio.gather(*tuple(kernel.tasks))
            return accepted, kernel, application

        accepted, kernel, application = asyncio.run(scenario())
        self.assertEqual([item.dedup_key for item in accepted], ["demo-1"])
        self.assertIn("demo-1", kernel._generic_completed)
        self.assertEqual(len(application.contexts), 1)
        self.assertNotIn(KernelEventKind.POLL_TICK, application.events)


if __name__ == "__main__":
    unittest.main()
