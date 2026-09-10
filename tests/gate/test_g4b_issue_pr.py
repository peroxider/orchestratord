"""G4b issue→PR 业务链路冒烟 — DESIGN_PR_GATE_TEST.md §5.5.1。

真实 daemon 上的完整业务链路（无外网、无真实 LLM、无真实 forge）：

    issue markdown 落盘（local tracker）→ kernel dispatch → fake codex
    shim 确定性回包（写文件 + codex exec --json 协议 JSONL）→ git sync
    commit（workspace.repo_clone_url 指向本地 bare 仓库）→ pending_review
    → CLI review approve → completed

local tracker 语义下无 push（git/sync.py is_local_tracker → no_push），
commit 落在 issue 工作区克隆内、PR 体现为 issue 文件 frontmatter——
断言相应落在克隆的 git log 与 registry 字段上。
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time

import gate_support as g
import pytest
import yaml

pytestmark = pytest.mark.gate  # PR merge gate (DESIGN_PR_GATE_TEST.md §4)

PENDING_REVIEW_BUDGET_S = 120  # DESIGN §5.5.1 预算

#: fake codex shim：吃掉 stdin 的 prompt，向 cwd 写确定性变更文件，
#: 再按 codex exec --json 协议输出最小成功 JSONL（session.py:_translate_wire_event）。
FAKE_CODEX_SHIM = """#!/usr/bin/env python3
import json
import os
import sys

sys.stdin.read()  # prompt arrives on stdin; deterministic stub ignores it
target = os.path.join(os.getcwd(), "g4b_change.md")
with open(target, "w", encoding="utf-8") as f:
    f.write("G4b deterministic stub change\\n")
for row in (
    {"type": "thread.started", "thread_id": "g4b-fake-thread"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "G4b stub change applied"},
    },
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
):
    print(json.dumps(row))
sys.exit(0)
"""

ISSUE_MARKDOWN = """---
state: open
---

# G4b smoke issue

Create a file named g4b_change.md containing the stub output.
"""


def _run(args, cwd, env, timeout=60):
    return subprocess.run(
        [str(a) for a in args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _make_issue_workflow(tmp, issues_path, origin_url) -> None:
    """经真实 CLI 生成 workflow，再把 frontmatter 改写为 G4b 链路配置。"""
    proc = _run(
        [
            g.CONSOLE_SCRIPT,
            "workflow",
            "init",
            "--template",
            "workflow-local",
            "--non-interactive",
            "--output",
            "w.md",
        ],
        cwd=tmp,
        env=g.gate_env(tmp),
    )
    assert proc.returncode == 0, f"workflow init failed:\n{proc.stdout}\n{proc.stderr}"
    wf = tmp / "w.md"
    m = re.match(r"(?s)^---\n(.*?)\n---\n(.*)$", wf.read_text(encoding="utf-8"))
    assert m, "workflow frontmatter 结构异常"
    cfg = yaml.safe_load(m.group(1))
    cfg["tracker"]["issues_path"] = str(issues_path)
    cfg["polling"]["interval_ms"] = 1000
    cfg["workspace"]["root"] = str(tmp / "orchestratord-workspace")
    cfg["workspace"]["repo_clone_url"] = origin_url
    wf.write_text(
        "---\n"
        + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
        + "---\n"
        + m.group(2),
        encoding="utf-8",
    )


def _load_registry(ws_root):
    path = ws_root / ".orchestratord_issue_registry.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_issue_status(ws_root, issue_id, statuses, budget):
    deadline = time.time() + budget
    last = None
    while time.time() < deadline:
        rec = _load_registry(ws_root).get(issue_id)
        if rec is not None:
            last = rec.get("status")
            if last in statuses:
                return rec
        time.sleep(0.5)
    raise AssertionError(
        f"issue {issue_id} 未在 {budget:.0f}s 内到达 {statuses}（最后状态: {last}）"
    )


@pytest.fixture(scope="module")
def g4b_chain(request, tmp_path_factory):
    """一次性拉起 daemon：bare origin + fake codex shim + local tracker。"""
    g.registered_skip(
        request,
        "G4b.issue_pr_chain",
        env_ok=g.pg_reachable(),
        env_gone="Postgres unreachable at 127.0.0.1:5432",
    )

    tmp = tmp_path_factory.mktemp("g4bchain")

    # 本地 bare 仓库充当 origin（clone 来源；local tracker 不 push）。
    # 预置一个种子提交：空仓库克隆无 HEAD，launch 阶段 git rev-parse HEAD
    # 会失败；-b main 与 registry 默认 base_branch 对齐。
    origin = tmp / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    (seed / "README.md").write_text("g4b seed\n", encoding="utf-8")
    git = lambda *a, **k: subprocess.run(  # noqa: E731
        ["git", "-C", str(seed), *a],
        check=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp)},
        **k,
    )
    git("add", "-A")
    git("-c", "user.name=gate", "-c", "user.email=gate@example.com", "commit", "-qm", "seed")
    git("push", "-q", "origin", "main")

    issues_path = tmp / "issues"
    issues_path.mkdir()
    (issues_path / "g4b-1.md").write_text(ISSUE_MARKDOWN, encoding="utf-8")

    _make_issue_workflow(tmp, issues_path, f"file://{origin}")

    # fake codex shim：PATH 最前，抢在 conftest guard shim（exit 126）与真实 CLI 之前
    shim_dir = tmp / "shims"
    shim_dir.mkdir()
    shim = shim_dir / "codex"
    shim.write_text(FAKE_CODEX_SHIM, encoding="utf-8")
    shim.chmod(0o755)

    env = g.gate_env(tmp)
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]

    port = g.free_port()
    proc, out, err = g.spawn(
        [
            g.CONSOLE_SCRIPT,
            "server",
            "start",
            "--workflow",
            "w.md",
            "--backend",
            "codex-cli",
            "--serve-api",
            "--api-port",
            str(port),
        ],
        cwd=tmp,
        env=env,
        name="g4bchain",
    )
    ws_root = tmp / "orchestratord-workspace"
    g.wait_health(f"http://127.0.0.1:{port}")
    yield tmp, ws_root

    g.terminate_gracefully(proc)
    offenders = [ln for log in (out, err) for ln in g.scan_log(log)]
    assert not offenders, "daemon 日志出现 Traceback:\n" + "\n".join(offenders[:20])


class TestG4b:
    def test_issue_to_commit_to_completed(self, g4b_chain) -> None:
        """issue → dispatch → backend → sync commit → review approve → completed。"""
        tmp, ws_root = g4b_chain

        # 1. 链路推进到人工检视点（local tracker：sync 后 pending_review）
        rec = _wait_issue_status(
            ws_root, "g4b-1", {"pending_review", "completed"}, PENDING_REVIEW_BUDGET_S
        )
        assert rec.get("commit_sha"), f"sync 未产生 commit_sha: {rec}"

        # 2. 预期 commit 出现在 issue 工作区克隆（local tracker 不 push）
        clones = sorted(glob.glob(str(ws_root / "*" / ".git")))
        assert clones, f"workspace root 下未找到 issue 克隆: {sorted(ws_root.iterdir())}"
        clone = clones[0].removesuffix("/.git")
        log = subprocess.run(
            ["git", "-C", clone, "log", "--format=%s", "--name-only", "-n", "1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "g4b_change.md" in log, f"克隆最新提交未含预期变更: {log!r}"

        # 3. 真实 CLI 人工检视通过 → 终态 completed
        env = dict(os.environ)
        env.update(g.gate_env(tmp))
        env["ORCHESTRATORD_WORKSPACE_ROOT"] = str(ws_root)
        review = _run(
            [g.CONSOLE_SCRIPT, "issue", "review", "--id", "g4b-1", "--approve"],
            cwd=tmp,
            env=env,
        )
        assert review.returncode == 0, (
            f"issue review rc={review.returncode}\n{review.stdout}\n{review.stderr}"
        )
        rec = _wait_issue_status(ws_root, "g4b-1", {"completed"}, 30)
        assert rec.get("commit_sha"), "completed 记录丢失 commit_sha"
