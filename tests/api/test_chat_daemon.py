"""Chat daemon wiring tests (§6.1d).

Unit coverage pins the ``orchestratord.chat_daemon`` production seam: the
``ProgressEvent`` → sink-protocol adapter, backend-name resolution
(session agent provider, then ``ORCHESTRATORD_CHAT_BACKEND``), the
``AgentTask`` shape handed to ``BackendRunner.run_task``, and the
start/stop lifecycle helpers. ``resolve_backend`` / ``BackendRunner``
are monkeypatched so the unit classes need no real backend or database.

:class:`TestUsageAggregation` additionally exercises the §7.3 run →
``usage_aggregates`` fold through the real ``BackendRunner.run_task`` on
the live ``orchestratord_test`` database (skipped when Postgres is
unreachable).

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.1d, §7.3.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestratord.agent.task import (
    AgentTask,
    ProgressEvent,
    ProgressEventKind,
)
from orchestratord.chat_daemon import (
    build_chat_runner_invoke,
    progress_event_adapter,
    start_chat_daemon,
    stop_chat_daemon,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class FakeBridge:
    """Records sink-protocol calls made by the adapter."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def on_text(self, text: str) -> None:
        self.calls.append(("text", text))

    def on_text_delta(self, text: str) -> None:
        self.calls.append(("delta", text))

    def on_tool_call(self, tool_name: str, call_id: str) -> None:
        self.calls.append(("tool_call", tool_name, call_id))

    def on_tool_result(self, call_id: str) -> None:
        self.calls.append(("tool_result", call_id))

    def on_turn_complete(self, event, session) -> None:
        self.calls.append(("turn",))

    def on_session_complete(self, event, session) -> None:
        self.calls.append(("session",))

    def on_error(self, message: str) -> None:
        self.calls.append(("error", message))


def _event(kind: ProgressEventKind, **kwargs) -> ProgressEvent:
    return ProgressEvent(kind=kind, task_id="t", **kwargs)


class _FakeAgent:
    provider = "agent-backend"


class _FakeSession:
    def __init__(self, agent) -> None:
        self._agent = agent

    async def get(self, model, key):
        return self._agent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeFactory:
    def __init__(self, agent) -> None:
        self._agent = agent

    def __call__(self):
        return _FakeSession(self._agent)


class _ExplodingFactory:
    """Session factory that raises — the claim loop must survive it."""

    def __call__(self):
        raise RuntimeError("no db in unit test")


@pytest.fixture
def patch_runner(monkeypatch):
    """Stub ``resolve_backend`` + ``BackendRunner``, recording construction."""
    state: dict = {}

    def fake_resolve(identifier, config=None, *, strict=False):
        state["backend_name"] = identifier
        return ("sentinel-backend", identifier)

    class FakeRunner:
        def __init__(self, backend, agent_config, sandbox_config, workspace_cfg):
            state["workspace_root"] = workspace_cfg.root
            state["runner"] = self

        async def run_task(self, task, *, progress_callback=None):
            state["task"] = task
            state["progress_callback"] = progress_callback
            # Behave like _TaskProgressBridge: emit ProgressEvents at the
            # callback rather than calling sink methods directly.
            progress_callback(
                _event(ProgressEventKind.TEXT, text="hello from backend")
            )

    monkeypatch.setattr(
        "orchestratord.backend_registry.resolve_backend", fake_resolve
    )
    monkeypatch.setattr(
        "orchestratord.backend_runner.BackendRunner", FakeRunner
    )
    return state


# ---------------------------------------------------------------------------
# progress_event_adapter
# ---------------------------------------------------------------------------


class TestProgressEventAdapter:
    def test_maps_text_and_delta(self) -> None:
        bridge = FakeBridge()
        adapter = progress_event_adapter(bridge)
        adapter(_event(ProgressEventKind.TEXT, text="a"))
        adapter(_event(ProgressEventKind.TEXT_DELTA, text="b"))
        assert bridge.calls == [("text", "a"), ("delta", "b")]

    def test_maps_tool_events(self) -> None:
        bridge = FakeBridge()
        adapter = progress_event_adapter(bridge)
        adapter(_event(ProgressEventKind.TOOL_CALL, tool_name="Bash", call_id="c1"))
        adapter(_event(ProgressEventKind.TOOL_RESULT, call_id="c1"))
        assert bridge.calls == [("tool_call", "Bash", "c1"), ("tool_result", "c1")]

    def test_maps_completion_and_error(self) -> None:
        bridge = FakeBridge()
        adapter = progress_event_adapter(bridge)
        adapter(_event(ProgressEventKind.TURN_COMPLETE))
        adapter(_event(ProgressEventKind.SESSION_COMPLETE))
        adapter(_event(ProgressEventKind.ERROR, message="boom"))
        assert bridge.calls == [("turn",), ("session",), ("error", "boom")]


# ---------------------------------------------------------------------------
# build_chat_runner_invoke
# ---------------------------------------------------------------------------


class TestChatRunnerInvoke:
    async def test_session_agent_provider_wins(self, patch_runner) -> None:
        factory = _FakeFactory(_FakeAgent())
        invoke = build_chat_runner_invoke(factory)
        session_row = SimpleNamespace(id=uuid4(), agent_id=uuid4(), workspace_id=uuid4())
        bridge = FakeBridge()

        await invoke(session_row, "do the thing", bridge)

        assert patch_runner["backend_name"] == "agent-backend"
        assert patch_runner["task"].kind == "chat"
        assert patch_runner["task"].prompt_override == "do the thing"
        assert patch_runner["task"].conversation_id == str(session_row.id)
        # The run_task callback is the adapter: ProgressEvents in, sink
        # calls out onto the bridge.
        assert bridge.calls == [("text", "hello from backend")]

    async def test_env_backend_fallback(self, patch_runner, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_CHAT_BACKEND", "env-backend")
        invoke = build_chat_runner_invoke(_FakeFactory(None))
        session_row = SimpleNamespace(id=uuid4(), agent_id=None, workspace_id=uuid4())

        await invoke(session_row, "prompt", FakeBridge())

        assert patch_runner["backend_name"] == "env-backend"

    async def test_unresolvable_backend_raises_lookup_error(
        self, patch_runner, monkeypatch
    ) -> None:
        monkeypatch.delenv("ORCHESTRATORD_CHAT_BACKEND", raising=False)
        invoke = build_chat_runner_invoke(_FakeFactory(None))
        session_row = SimpleNamespace(id=uuid4(), agent_id=None, workspace_id=uuid4())

        with pytest.raises(LookupError):
            await invoke(session_row, "prompt", FakeBridge())

    async def test_workspace_root_from_env(
        self, patch_runner, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("ORCHESTRATORD_CHAT_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("ORCHESTRATORD_CHAT_BACKEND", "env-backend")
        invoke = build_chat_runner_invoke(_FakeFactory(None))
        session_row = SimpleNamespace(id=uuid4(), agent_id=None, workspace_id=uuid4())

        await invoke(session_row, "prompt", FakeBridge())

        assert patch_runner["workspace_root"] == str(tmp_path)


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


class TestDaemonLifecycle:
    async def test_start_spawns_loop_and_stop_ends_it(self) -> None:
        dispatcher = start_chat_daemon(
            session_factory=_ExplodingFactory(), interval=0.01
        )
        task = getattr(dispatcher, "_loop_task", None)
        assert task is not None and not task.done()
        await asyncio.sleep(0.05)  # a claim attempt fails; loop stays alive
        assert not task.done()
        await stop_chat_daemon(dispatcher)
        assert task.done()

    async def test_stop_is_idempotent(self) -> None:
        dispatcher = start_chat_daemon(
            session_factory=_ExplodingFactory(), interval=0.01
        )
        await stop_chat_daemon(dispatcher)
        await stop_chat_daemon(dispatcher)


# ---------------------------------------------------------------------------
# run → usage_aggregates fold (§7.3, live DB)
# ---------------------------------------------------------------------------


class TestUsageAggregation:
    pytestmark = pytest.mark.database

    def _runner(self):
        from orchestratord.backend_runner import BackendRunner
        from orchestratord.config.schema import AgentConfig, SandboxConfig

        runner = object.__new__(BackendRunner)
        runner.agent_config = AgentConfig(model="gpt-test")
        runner.sandbox_config = SandboxConfig()
        runner.workspace_cfg = None
        runner.backend = SimpleNamespace(name="stub")
        return runner

    async def test_completed_run_lands_usage_row(
        self, client, db_engine, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from orchestratord.api import db as api_db
        from orchestratord.db import models as orm
        from orchestratord.db.engine import build_session_factory

        monkeypatch.setattr(
            api_db, "_session_factory", build_session_factory(db_engine)
        )
        runner = self._runner()

        async def _fake_run(session, workflow, **kwargs):  # noqa: ANN003
            session.backend_name = "stub"
            session.token_usage = {"input": 120, "output": 40}
            # No backend-reported cost → the §7.2 estimator decides.
            session.cost_usd = 0.0

        runner.run = _fake_run

        ws_id, agent_id = uuid4(), uuid4()
        task = AgentTask(
            id="chat-usage-1",
            kind="chat",
            title="t",
            description="d",
            context={
                "workspace_id": str(ws_id),
                "agent_id": str(agent_id),
                "issue_id": None,
            },
        )
        await runner.run_task(task)

        factory = build_session_factory(db_engine)
        async with factory() as db:
            rows = (
                (await db.execute(select(orm.UsageAggregate)))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row.workspace_id == ws_id
        assert row.agent_id == agent_id
        assert row.backend == "stub"
        assert row.tokens_in == 120
        assert row.tokens_out == 40
        assert row.sessions == 1

    async def test_usage_failure_does_not_fail_run(
        self, db_engine, monkeypatch
    ) -> None:
        from orchestratord.api import db as api_db
        from orchestratord.db.engine import build_session_factory

        monkeypatch.setattr(
            api_db, "_session_factory", build_session_factory(db_engine)
        )

        def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("pricing service exploded")

        monkeypatch.setattr(
            "orchestratord.cost.estimator.estimate_cost_usd", _boom
        )
        runner = self._runner()

        async def _fake_run(session, workflow, **kwargs):  # noqa: ANN003
            session.backend_name = "stub"
            session.status = "completed"
            session.token_usage = {"input": 10, "output": 5}
            session.cost_usd = 0.0

        runner.run = _fake_run

        task = AgentTask(
            id="chat-usage-2",
            kind="chat",
            title="t",
            description="d",
            context={"workspace_id": str(uuid4())},
        )
        # Best-effort ingestion: the run itself must still succeed.
        result = await runner.run_task(task)
        assert result.status == "completed"
