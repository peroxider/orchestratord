"""G4 功能冒烟 — DESIGN_PR_GATE_TEST.md §5.5/§5.5.1。

活 daemon 上的真实 HTTP CRUD 往返（证明"允许常规操作时不崩溃"）+
echo 应用协议环（机制层 U5）。G4b issue_pr 业务链路按 §9 豁免登记
（P2 待实施），以 SKIP(registered) 显式可见。
"""
from __future__ import annotations

import asyncio

import os

import gate_support as g
import pytest

pytestmark = pytest.mark.gate  # PR merge gate (DESIGN_PR_GATE_TEST.md §4)


@pytest.fixture(scope="module")
def live_daemon(request, tmp_path_factory):
    """一次性拉起 daemon（--serve-api），建 schema + 种子 default 工作区。"""
    g.registered_skip(
        request,
        "G4.functional",
        env_ok=g.pg_reachable(),
        env_gone="Postgres unreachable at 127.0.0.1:5432",
    )
    old_dsn = os.environ.get("ORCHESTRATORD_DATABASE_URL")
    os.environ["ORCHESTRATORD_DATABASE_URL"] = g.GATE_DSN

    async def _prepare_db() -> None:
        from orchestratord.db import models  # noqa: F401 — 注册全部表
        from orchestratord.db.engine import (
            build_engine,
            build_session_factory,
            create_schema,
        )
        from orchestratord.seed import seed_default_workspace

        await g.recreate_db(g.GATE_DB)
        engine = build_engine(g.GATE_DSN)
        try:
            await create_schema(engine)
            await seed_default_workspace(build_session_factory(engine))
        finally:
            await engine.dispose()

    asyncio.run(_prepare_db())

    tmp = tmp_path_factory.mktemp("g4daemon")
    g.make_workflow(tmp)
    port = g.free_port()
    proc, out, err = g.spawn(
        [
            g.CONSOLE_SCRIPT,
            "server",
            "start",
            "--workflow",
            "w.md",
            "--backend",
            "codex-cli",  # preflight 仅做 PATH 存在性检查（stub shim 提供）
            "--serve-api",
            "--api-port",
            str(port),
        ],
        cwd=tmp,
        env=g.gate_env(tmp),
        name="g4daemon",
    )
    base = f"http://127.0.0.1:{port}"
    g.wait_health(base)
    yield base

    g.terminate_gracefully(proc)
    offenders = [ln for log in (out, err) for ln in g.scan_log(log)]
    assert not offenders, "daemon 日志出现 Traceback:\n" + "\n".join(offenders[:20])
    if old_dsn is None:
        os.environ.pop("ORCHESTRATORD_DATABASE_URL", None)
    else:
        os.environ["ORCHESTRATORD_DATABASE_URL"] = old_dsn


class TestG4:
    def test_issue_crud_roundtrip(self, live_daemon) -> None:
        """核心资源 create → read → update → list 过滤，真实 HTTP。"""
        import httpx

        base = live_daemon
        with httpx.Client(base_url=base, timeout=10) as c:
            ws = c.get("/api/workspaces/by-slug/default")
            assert ws.status_code == 200, f"种子工作区不可达: {ws.text}"
            wid = ws.json()["workspace_id"]

            created = c.post(
                f"/api/workspaces/{wid}/issues",
                json={"title": "gate-smoke", "description": "PR gate CRUD roundtrip"},
            )
            assert created.status_code == 201, created.text
            iid = created.json()["id"]

            got = c.get(f"/api/workspaces/{wid}/issues/{iid}")
            assert got.status_code == 200 and got.json()["title"] == "gate-smoke"

            patched = c.patch(
                f"/api/workspaces/{wid}/issues/{iid}", json={"status": "ready"}
            )
            assert patched.status_code == 200, patched.text

            listed = c.get(
                f"/api/workspaces/{wid}/issues", params={"status": "ready"}
            )
            assert listed.status_code == 200
            assert any(i["id"] == iid for i in listed.json())

    def test_echo_protocol_loop(self) -> None:
        """echo 应用协议环（机制层）：poll → prepare_run → single mode → interpret。"""
        from pathlib import Path
        from types import SimpleNamespace

        from orchestratord.agent.task import AgentTaskResult
        from orchestratord.applications.echo.lifecycle import EchoLifecycle
        from orchestratord.applications.echo.provider import EchoWorkProvider
        from orchestratord.kernel.application import Outcome
        from orchestratord.kernel.run_context import RunContext
        from orchestratord.modes.single import SingleModeRunner
        from orchestratord.session_state import RunSession, RunSubject

        class _StubRunner:
            async def run(self, session, workflow, **hooks):
                return AgentTaskResult(
                    task_id=session.task.id,
                    kind=session.task.kind,
                    status="completed",
                    output_text=f"ECHO:{session.task.id}",
                )

        run = asyncio.run
        provider = EchoWorkProvider()
        lifecycle = EchoLifecycle()
        mode = SingleModeRunner(_StubRunner())

        items = run(provider.poll())
        assert [i.dedup_key for i in items] == ["echo-1", "echo-2", "echo-3"]
        for item in items:
            ctx = RunContext.from_task(item.task)
            prepared = run(lifecycle.prepare_run(item, ctx))
            session = RunSession(
                subject=RunSubject(id=item.dedup_key),
                workspace=SimpleNamespace(path=Path(".")),
                task=item.task,
                prompt_override=prepared.prompt,
            )
            result = run(mode.run(session, None))
            outcome = run(lifecycle.interpret_result(item, result, ctx))
            assert isinstance(outcome, Outcome)
            assert outcome.kind == "dispose"
        assert run(provider.poll()) == [], "poll must be exhausted after delivery"
