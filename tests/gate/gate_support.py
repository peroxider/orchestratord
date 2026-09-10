"""Shared support for the PR merge-gate suite — DESIGN_PR_GATE_TEST.md §5/§9.

Imported by gate test modules as ``import gate_support as g`` (the
``tests/gate`` directory is on pytest's ``pythonpath`` — see pyproject.toml).
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_SCRIPT = Path(sys.executable).parent / "orchestratord"

GATE_DB = "orchestratord_gate"
PG_ADMIN_DSN = "postgresql://multica:multica@127.0.0.1:5432/multica"
GATE_DSN = f"postgresql+asyncpg://multica:multica@127.0.0.1:5432/{GATE_DB}"

HEALTH_BUDGET_S = 30
SHUTDOWN_BUDGET_S = 15

#: Log fragments that indicate a user-facing crash (DESIGN §6 健康判据).
LOG_SCAN_PATTERNS = ("Traceback (most recent call last)",)


# ---------------------------------------------------------------------------
# 豁免登记表（DESIGN §9）——唯一事实源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateExemption:
    check_id: str
    reason: str
    env_gone_condition: str
    expires: str  # 绝对日期 YYYY-MM-DD；过期即 FAIL
    platform: str | None = None  # 平台限定；None = 全平台


GATE_EXEMPTIONS: tuple[GateExemption, ...] = (
    GateExemption(
        check_id="G3.graceful_sigterm_exit_code",
        reason=(
            "Windows 原生无 SIGTERM；放宽为 psutil 递归终止 + 无存活子进程 "
            "+ 日志无未处理异常（DESIGN §5.4 平台差异）"
        ),
        env_gone_condition="不适用——平台能力差异，非环境缺失",
        expires="2027-09-09",
        platform="win32",
    ),
    GateExemption(
        check_id="G4b.issue_pr_chain",
        reason=(
            "P2 待实施：issue→PR 业务链路需 fake git remote + 确定性 stub "
            "回包 fixtures（DESIGN §5.5.1）"
        ),
        env_gone_condition="P2 落地后撤销本条目",
        expires="2026-10-31",
    ),
    GateExemption(
        check_id="G2.migrations",
        reason=(
            "真实缺陷：迁移 0009 对分区表 events 执行 CREATE INDEX "
            "CONCURRENTLY，PG 拒绝（cannot create index on partitioned "
            "table ... concurrently），全新库 alembic upgrade head 必败。"
            "修复方向：去掉 CONCURRENTLY，或 CREATE INDEX ON ONLY + 各分区"
        ),
        env_gone_condition="0009 修复后撤销本条目",
        expires="2026-10-31",
    ),
)


def exemption_for(check_id: str) -> GateExemption | None:
    return next((e for e in GATE_EXEMPTIONS if e.check_id == check_id), None)


def _expiry(e: GateExemption) -> date:
    return date.fromisoformat(e.expires)


def registered_skip(
    request: pytest.FixtureRequest, check_id: str, *, env_ok: bool, env_gone: str
) -> None:
    """DESIGN §3/§9 fail-closed skip: 环境缺失必须登记豁免才能 SKIP。

    env_ok=True  → 正常执行（豁免被无视）。
    env_ok=False → 未登记或已过期即 FAIL；已登记且未过期 → SKIP(registered)。
    环境敏感豁免的防腐烂由过期日期 + nightly 全量对拍兜底（DESIGN §9）。
    """
    if env_ok:
        return
    e = exemption_for(check_id)
    if e is None:
        pytest.fail(
            f"{check_id}: 环境缺失（{env_gone}）且无豁免登记 — fail-closed（DESIGN §9）"
        )
    if date.today() > _expiry(e):
        pytest.fail(f"{check_id}: 豁免已过期（{e.expires}）——应修复环境或撤销豁免")
    if e.platform is not None and e.platform != sys.platform:
        pytest.fail(
            f"{check_id}: 豁免限定平台 {e.platform} 却被 {sys.platform} 引用"
        )
    pytest.skip(f"SKIP(registered) {check_id}: {env_gone} — {e.reason}")


def assert_graceful_sigterm_exit(proc: subprocess.Popen) -> None:
    """POSIX: 断言 SIGTERM 后退出码 0。win32: 引用已登记豁免后放宽（§5.4）。"""
    if sys.platform == "win32":
        e = exemption_for("G3.graceful_sigterm_exit_code")
        assert e is not None, "win32 平台必须登记 G3.graceful_sigterm_exit_code 豁免"
        assert date.today() <= _expiry(e), "G3.graceful_sigterm_exit_code 豁免已过期"
        return  # 断言放宽：进程清理由 kill_tree 兜底，健康性由日志扫描判定
    assert proc.returncode == 0, (
        f"expected exit 0 after SIGTERM, got {proc.returncode}"
    )


def waive_known_defect(
    request: pytest.FixtureRequest, check_id: str, *, output: str, signature: str
) -> None:
    """已知缺陷豁免（DESIGN §8/§9）：失败输出含 signature 且登记未过期
    → SKIP(registered) 显式可见；未登记或过期 → 调用方按普通失败 FAIL。"""
    if signature not in output:
        return
    e = exemption_for(check_id)
    if e is None:
        return
    if date.today() > _expiry(e):
        pytest.fail(f"{check_id}: 豁免已过期（{e.expires}）——缺陷应已修复或更新登记")
    pytest.skip(f"SKIP(registered) {check_id}: 已知缺陷 — {e.reason}")


# ---------------------------------------------------------------------------
# Postgres 探测 / 门禁库管理
# ---------------------------------------------------------------------------


def pg_reachable(timeout: float = 3.0) -> bool:
    try:
        import asyncpg

        async def _probe() -> None:
            conn = await asyncpg.connect(PG_ADMIN_DSN, timeout=timeout)
            await conn.close()

        asyncio.run(_probe())
        return True
    except Exception:
        return False


async def recreate_db(dbname: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(PG_ADMIN_DSN, timeout=5)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", dbname
        )
        if exists:
            await conn.execute(f"DROP DATABASE {dbname} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {dbname}")
    finally:
        await conn.close()


async def schema_snapshot(dsn: str) -> dict[str, set[str]]:
    """public schema 的 {table: {column}} 快照（G2 迁移/ORM 对拍用）。"""
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        tables = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        }
        out: dict[str, set[str]] = {}
        for t in tables:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1",
                t,
            )
            out[t] = {r["column_name"] for r in rows}
        return out
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 真实子进程设施（DESIGN §3 Real-Process-First）
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def gate_env(home: Path, dsn: str = GATE_DSN) -> dict[str, str]:
    """子进程环境：HOME 隔离 ~/.orchestratord 元数据 + 门禁库 DSN。"""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["ORCHESTRATORD_DATABASE_URL"] = dsn
    return env


def spawn(
    args: list[str], *, cwd: Path, env: dict[str, str], name: str
) -> tuple[subprocess.Popen, Path, Path]:
    out = cwd / f".gate-{name}.out.log"
    err = cwd / f".gate-{name}.err.log"
    proc = subprocess.Popen(
        [str(a) for a in args],
        cwd=str(cwd),
        env=env,
        stdout=open(out, "wb"),
        stderr=open(err, "wb"),
    )
    return proc, out, err


def kill_tree(proc: subprocess.Popen) -> None:
    """psutil 递归终止，teardown 兜底不留孤儿进程（DESIGN §5.4）。"""
    try:
        import psutil
    except ImportError:
        proc.kill()
        return
    try:
        parent = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for child in children:
        child.kill()
    parent.kill()
    psutil.wait_procs([parent], timeout=5)


def scan_log(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [ln for ln in lines if any(p in ln for p in LOG_SCAN_PATTERNS)]


def wait_health(base: str, budget: float = HEALTH_BUDGET_S) -> None:
    """轮询 /api/health 至 200，超预算即 FAIL（DESIGN §6 时延判据）。"""
    import urllib.request

    deadline = time.time() + budget
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.25)
    raise AssertionError(f"/api/health not ready within {budget:.0f}s (last: {last})")


def make_workflow(tmp: Path) -> Path:
    """经真实 CLI 生成 workflow 文件（用户路径），返回 w.md 路径。"""
    proc = subprocess.run(
        [
            str(CONSOLE_SCRIPT),
            "workflow",
            "init",
            "--template",
            "workflow-local",
            "--non-interactive",
            "--output",
            "w.md",
        ],
        cwd=str(tmp),
        env=gate_env(tmp),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"workflow init failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    wf = tmp / "w.md"
    assert wf.is_file(), "workflow init 未产出 w.md"
    # 每测试唯一 workspace root：workflow-local 模板默认 root 是共享路径
    # （/tmp/orchestratord_workspaces/...），会带来 slug 碰撞与跨测试状态
    # 泄漏（#34 教训）。
    root = tmp / "orchestratord-workspace"
    root.mkdir(parents=True, exist_ok=True)
    text = wf.read_text(encoding="utf-8")
    text = re.sub(
        r"(?m)^(\s*root:\s*).*$",
        lambda m: m.group(1) + '"' + str(root).replace("\\", "/") + '"',
        text,
        count=1,
    )
    wf.write_text(text, encoding="utf-8")
    return wf


def terminate_gracefully(
    proc: subprocess.Popen, budget: float = SHUTDOWN_BUDGET_S
) -> None:
    """SIGTERM → 预算内等待；超时 kill_tree 兜底。"""
    if proc.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            proc.send_signal(signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=budget)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
