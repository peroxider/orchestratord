"""业务无关的 Orchestration Kernel。

Kernel 只负责四件事：轮询节奏、并发槽位、运行生命周期和 Outcome
消费。业务应用通过 ``Application.on_kernel_event`` 接收轮询事件，
``WorkProvider`` 负责提供工作项；这使得 issue→PR 不再需要把业务副
循环塞进一个巨型 Orchestrator 主循环。

The ``runtime`` adapter is intentionally structural.  It exists for the
legacy issue-to-PR composition root while the generic path below is usable
by new applications without importing that composition root.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import time
from typing import Any

from ..session_state import RetryItem
from .application import Application, Outcome, PreparedRun
from .dispatch import OrchestratorState
from .events import KernelEvent, KernelEventKind, KernelHooks
from .run_context import RunContext
from .work_provider import WorkItem, WorkProvider

logger = logging.getLogger(__name__)


class OrchestrationKernel:
    """Run loop and generic dispatch implementation.

    ``runtime`` is used by the existing issue→PR composition root.  When it
    is omitted, ``application`` + ``work_provider`` + ``runner`` form a
    complete generic kernel and can be exercised independently.
    """

    def __init__(
        self,
        *,
        runtime: Any | None = None,
        application: Application | None = None,
        work_provider: WorkProvider | None = None,
        runner: Any | None = None,
        workflow: Any | None = None,
        state: OrchestratorState | None = None,
        shutdown_event: asyncio.Event | None = None,
        poll_interval_ms: int = 30_000,
        max_concurrent_agents: int = 10,
        hooks: KernelHooks | None = None,
    ) -> None:
        self.runtime = runtime
        self.application = application
        self.work_provider = work_provider
        self.runner = runner
        self.workflow = workflow
        self.state = state or OrchestratorState(
            poll_interval_ms=poll_interval_ms,
            max_concurrent_agents=max_concurrent_agents,
        )
        self.shutdown_event = shutdown_event or asyncio.Event()
        self.hooks = hooks
        self.semaphore = asyncio.Semaphore(self.state.max_concurrent_agents)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._generic_running: dict[str, WorkItem] = {}
        self._generic_completed: set[str] = set()
        self._generic_retry_queue: list[RetryItem] = []
        self._generic_retry_items: dict[str, WorkItem] = {}
        self._generic_spawn_queue: list[WorkItem] = []
        self.outcomes: dict[str, Outcome] = {}

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _notify(self, event: KernelEvent) -> None:
        app = self.application
        if app is None and self.runtime is not None:
            app = getattr(self.runtime, "_issue_app", None)
        if app is None:
            return
        handler = getattr(app, "on_kernel_event", None)
        if handler is not None:
            await self._maybe_await(handler(event))

    async def dispatch_once(self) -> Any:
        """Run one daemon poll cycle.

        The method is public so tests and embedded hosts can drive the kernel
        deterministically without starting a background daemon.
        """
        if self.runtime is not None:
            return await self._dispatch_runtime_once()
        return await self._dispatch_generic_once()

    async def _dispatch_loop(self) -> Any:
        """Named internal loop seam retained for embedders and diagnostics."""
        return await self.dispatch_once()

    async def _dispatch_runtime_once(self) -> None:
        runtime = self.runtime
        dashboard = runtime.status_dashboard
        dashboard.on_poll_start()
        self.state.poll_check_in_progress = True
        try:
            runtime._refresh_dynamic_title_prefix_filter()
            await runtime._process_control_commands()

            # Keep the historical order: clarification answers first, retry
            # queue second, then the remaining application poll cycles.
            await self._notify(
                KernelEvent(
                    KernelEventKind.POLL_TICK,
                    payload={"phase": "before_retry"},
                )
            )
            await runtime._process_retry_queue()
            await self._notify(
                KernelEvent(
                    KernelEventKind.POLL_TICK,
                    payload={"phase": "after_retry"},
                )
            )
            await runtime._work_provider.poll()
        finally:
            self.state.poll_check_in_progress = False
            dashboard.on_poll_end()
            runtime._broadcast_clarification_status()

    async def _dispatch_generic_once(self) -> list[WorkItem]:
        if self.work_provider is None:
            return []
        now = time.time()
        ready_retries: list[WorkItem] = []
        not_ready: list[RetryItem] = []
        for retry in self._generic_retry_queue:
            if now >= retry.scheduled_at + retry.delay_seconds:
                retry_item = self._generic_retry_items.pop(retry.dedup_key, None)
                if retry_item is not None:
                    ready_retries.append(retry_item)
            else:
                not_ready.append(retry)
        self._generic_retry_queue = not_ready
        items = [*self._generic_spawn_queue, *ready_retries, *(await self.work_provider.poll())]
        self._generic_spawn_queue.clear()
        accepted: list[WorkItem] = []
        for item in sorted(items, key=lambda candidate: -candidate.priority):
            key = item.dedup_key
            if not key or key in self._generic_running or key in self._generic_completed:
                await self.work_provider.on_dispatch_rejected(item, "duplicate or terminal")
                continue
            if len(self._generic_running) >= self.state.max_concurrent_agents:
                await self.work_provider.on_dispatch_rejected(item, "concurrency limit")
                continue
            task = asyncio.create_task(self._execute(item))
            self.tasks.add(task)
            self.run_tasks[key] = task
            self._generic_running[key] = item
            task.add_done_callback(self.tasks.discard)
            task.add_done_callback(lambda done, dedup_key=key: self.run_tasks.pop(dedup_key, None))
            accepted.append(item)
        return accepted

    async def _execute(self, item: WorkItem) -> Outcome:
        """Execute one generic WorkItem and consume its application Outcome."""
        if self.application is None or self.runner is None or item.task is None:
            outcome = Outcome.dispose("unexecutable_work_item")
            await self._apply_outcome(item, outcome)
            self._generic_running.pop(item.dedup_key, None)
            return outcome

        async with self.semaphore:
            ctx = RunContext.from_task(item.task)
            prepared = await self.application.prepare_run(item, ctx)
            if prepared is None:
                outcome = Outcome.wait_external("application_gated")
                await self._apply_outcome(item, outcome)
                self._generic_running.pop(item.dedup_key, None)
                return outcome
            ctx.business.update(prepared.business)
            task = self._prepared_task(item.task, prepared)
            ctx = RunContext.from_task(task)
            ctx.business.update(prepared.business)
            await self._notify(
                KernelEvent(
                    KernelEventKind.RUN_STARTED,
                    dedup_key=item.dedup_key,
                    run_id=ctx.run_id,
                )
            )
            try:
                selected_runner = prepared.runner_override or self.runner
                result = await selected_runner.run_task(task)
                outcome = await self.application.interpret_result(item, result, ctx)
                await self._apply_outcome(item, outcome)
                await self._notify(
                    KernelEvent(
                        KernelEventKind.RUN_FINISHED,
                        dedup_key=item.dedup_key,
                        run_id=result.run_id or ctx.run_id,
                        payload={"status": result.status},
                    )
                )
                return outcome
            except asyncio.CancelledError:
                await self._notify(
                    KernelEvent(
                        KernelEventKind.RUN_CANCELLED,
                        dedup_key=item.dedup_key,
                        run_id=ctx.run_id,
                    )
                )
                raise
            finally:
                self._generic_running.pop(item.dedup_key, None)

    @staticmethod
    def _prepared_task(task: Any, prepared: PreparedRun) -> Any:
        """Apply application execution hints without mutating the provider item."""
        copy_task = copy.copy(task)
        if prepared.prompt is not None:
            copy_task.prompt_override = prepared.prompt
        if prepared.max_turns is not None:
            copy_task.max_turns = prepared.max_turns
        if prepared.timeout_seconds is not None:
            copy_task.timeout_seconds = prepared.timeout_seconds
        return copy_task

    async def _apply_outcome(self, item: WorkItem, outcome: Outcome) -> None:
        """Apply only generic lifecycle semantics; business meaning stays opaque."""
        kind = outcome.kind
        if kind == "dispose":
            self._generic_completed.add(item.dedup_key)
        elif kind == "retry":
            self._generic_retry_queue.append(
                RetryItem(
                    dedup_key=item.dedup_key,
                    attempt=item.task.attempt if item.task is not None else 1,
                    delay_seconds=max(0.0, outcome.retry_delay or 0.0),
                )
            )
            self._generic_retry_items[item.dedup_key] = item
        elif kind == "spawn" and outcome.spawn_item is not None and self.work_provider is not None:
            # Spawned work is queued for the next kernel tick.  The provider
            # remains the source of externally persisted work; this queue is
            # the in-memory hand-off for a single dispatch lineage.
            self._generic_spawn_queue.append(outcome.spawn_item)
        elif kind == "wait_external":
            # Waiting is represented by the absence of an active run; the
            # application owns the external subscription and re-poll policy.
            return
        else:
            logger.warning("Unknown application outcome kind=%s", kind)

    async def consume_outcome(self, item: WorkItem, outcome: Outcome) -> None:
        """Record and apply an outcome produced by a runtime adapter.

        The legacy issue runner performs domain side effects while
        interpreting a session.  This explicit hand-off still makes Kernel
        the owner of the lifecycle decision and gives the composition root a
        single place to attach runtime-specific scheduling.
        """
        self.outcomes[item.dedup_key] = outcome
        if self.runtime is not None:
            callback = getattr(self.runtime, "_kernel_apply_outcome", None)
            if callback is not None:
                await self._maybe_await(callback(item, outcome))
            return
        await self._apply_outcome(item, outcome)

    async def run(self) -> None:
        """Run the daemon loop or the generic application loop."""
        if self.runtime is not None:
            await self._run_runtime()
            return
        await self._run_generic()

    async def _run_runtime(self) -> None:
        runtime = self.runtime
        started_at = await runtime._kernel_start()
        heartbeat = asyncio.create_task(runtime._metadata_heartbeat_loop())
        self.tasks.add(heartbeat)
        await self._notify(KernelEvent(KernelEventKind.KERNEL_STARTED))
        exit_status = 0
        try:
            while not self.shutdown_event.is_set():
                await self._dispatch_loop()
                try:
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=self.state.poll_interval_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    pass
            await runtime._cancel_all_tasks()
        except Exception as exc:
            exit_status = 1
            await runtime._kernel_record_error(exc)
            raise
        finally:
            await runtime._kernel_finish(started_at, exit_status)

    async def _run_generic(self) -> None:
        await self._notify(KernelEvent(KernelEventKind.KERNEL_STARTED))
        while not self.shutdown_event.is_set():
            await self._dispatch_loop()
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=self.state.poll_interval_ms / 1000.0,
                )
            except asyncio.TimeoutError:
                pass

    async def shutdown(self) -> None:
        self.shutdown_event.set()
        for task in tuple(self.tasks):
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


__all__ = ["OrchestrationKernel"]
