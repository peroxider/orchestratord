"""G3 启动冒烟 — DESIGN_PR_GATE_TEST.md §5.4。

真实子进程拉起 daemon / uvicorn serve，轮询 /api/health，SIGTERM 优雅退出，
全程日志扫描。本层是设计文档的核心增量：现有测试从不真正绑定 socket。
"""
from __future__ import annotations

import json

import subprocess
import threading

import urllib.request

import gate_support as g
import pytest

pytestmark = pytest.mark.gate  # PR merge gate (DESIGN_PR_GATE_TEST.md §4)

#: /openapi.json paths 数下限基线（基线更新须独立 commit — DESIGN §6）。
OPENAPI_PATH_COUNT_FLOOR = 25

#: ``server start`` 强制要求 --backend；codex-cli 的 preflight 只做
#: ``shutil.which("codex")`` 存在性检查——测试 conftest 的 stub shim
#: 恰好提供，daemon 不会真正拉起 agent CLI（门禁不派发任务）。
GATE_BACKEND = "codex-cli"


def _require_pg(request: pytest.FixtureRequest) -> None:
    g.registered_skip(
        request,
        "G3.startup",
        env_ok=g.pg_reachable(),
        env_gone="Postgres unreachable at 127.0.0.1:5432",
    )


@pytest.fixture()
def gate_db_ready(request):
    """门禁库就绪：真实部署等价物（已迁移/建表的库）。"""
    _require_pg(request)
    import asyncio

    async def _prep() -> None:
        from orchestratord.db import models  # noqa: F401 — 注册全部表
        from orchestratord.db.engine import build_engine, create_schema

        await g.recreate_db(g.GATE_DB)
        engine = build_engine(g.GATE_DSN)
        try:
            await create_schema(engine)
        finally:
            await engine.dispose()

    asyncio.run(_prep())


def _start_daemon(tmp, port: int, name: str = "daemon"):
    wf = g.make_workflow(tmp)
    assert wf.is_file()
    return g.spawn(
        [
            g.CONSOLE_SCRIPT,
            "server",
            "start",
            "--workflow",
            "w.md",
            "--backend",
            GATE_BACKEND,
            "--serve-api",
            "--api-port",
            str(port),
        ],
        cwd=tmp,
        env=g.gate_env(tmp),
        name=name,
    )


def _assert_no_traceback(logs: list) -> None:
    offenders = [ln for log in logs for ln in g.scan_log(log)]
    assert not offenders, "子进程日志出现 Traceback:\n" + "\n".join(offenders[:20])


class TestG3:
    def test_daemon_start_health_sigterm(self, gate_db_ready, tmp_path) -> None:
        port = g.free_port()
        proc, out, err = _start_daemon(tmp_path, port)
        try:
            base = f"http://127.0.0.1:{port}"
            g.wait_health(base)
            with urllib.request.urlopen(f"{base}/openapi.json", timeout=5) as r:
                spec = json.load(r)
            assert len(spec.get("paths", {})) >= OPENAPI_PATH_COUNT_FLOOR, (
                f"openapi paths={len(spec.get('paths', {}))} "
                f"低于基线 {OPENAPI_PATH_COUNT_FLOOR}"
            )
        finally:
            g.terminate_gracefully(proc)
        g.assert_graceful_sigterm_exit(proc)
        _assert_no_traceback([out, err])

    def test_serve_variant(self, gate_db_ready, tmp_path) -> None:
        """uvicorn serve 路径（--no-seed）：补 fake-uvicorn 测试的 socket 盲区。"""
        port = g.free_port()
        proc, out, err = g.spawn(
            [g.CONSOLE_SCRIPT, "serve", "--port", str(port), "--no-seed"],
            cwd=tmp_path,
            env=g.gate_env(tmp_path),
            name="serve",
        )
        try:
            g.wait_health(f"http://127.0.0.1:{port}")
        finally:
            g.terminate_gracefully(proc)
        # uvicorn ≥0.52 的 capture_signals 在优雅关闭完成后重放捕获的
        # SIGTERM，进程以 -15 结束属 uvicorn 标准语义（实测确认：日志含
        # 完整关闭序列）。判据：退出码 0/-15 + 优雅关闭日志 + 无 Traceback。
        assert proc.returncode in (0, -15), f"serve 退出码 {proc.returncode}"
        # uvicorn 的生命周期日志走 stderr，访问日志走 stdout——合并扫描
        combined = "\n".join(
            p.read_text(encoding="utf-8", errors="replace") for p in (out, err)
        )
        assert "Application shutdown complete" in combined, (
            "未见 uvicorn 优雅关闭完成日志:\n" + combined[-1500:]
        )
        _assert_no_traceback([out, err])

    def test_server_stop_variant(self, gate_db_ready, tmp_path) -> None:
        """start → server stop：PID 文件清理、SIGTERM 转发、不误杀（#34 门禁化）。"""
        port = g.free_port()
        proc, out, err = _start_daemon(tmp_path, port, name="stop-variant")
        ws_root = tmp_path / "orchestratord-workspace"  # make_workflow 写入的 root
        try:
            g.wait_health(f"http://127.0.0.1:{port}")
            # 僵尸进程伪影：daemon 是本测试进程的直接子进程，退出后变僵尸，
            # 而 server stop（独立进程）的 _is_pid_alive 用 signal 0 探测，
            # 僵尸仍报"存活"→ stop 误判超时 rc=1（真实场景 daemon 被 init
            # 收割无此问题）。后台收割线程模拟 init reap。
            reaper = threading.Thread(target=proc.wait, daemon=True)
            reaper.start()
            stop = subprocess.run(
                [
                    str(g.CONSOLE_SCRIPT),
                    "server",
                    "stop",
                    "--workspace",
                    str(ws_root),
                ],
                cwd=str(tmp_path),
                env=g.gate_env(tmp_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert stop.returncode == 0, (
                f"server stop rc={stop.returncode}\n{stop.stdout}\n{stop.stderr}"
            )
            # daemon 应在预算内被 stop 优雅终止
            try:
                proc.wait(timeout=g.SHUTDOWN_BUDGET_S)
            except subprocess.TimeoutExpired:
                g.kill_tree(proc)
                raise AssertionError("server stop 后 daemon 未在预算内退出")
        finally:
            g.terminate_gracefully(proc)
        g.assert_graceful_sigterm_exit(proc)
        assert not (ws_root / "daemon.pid").exists(), "daemon.pid 未被清理"
        _assert_no_traceback([out, err])
