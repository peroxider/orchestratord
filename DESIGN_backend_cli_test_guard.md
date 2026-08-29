# 后端 CLI 测试挡板方案 — 设计文档

> **状态：** 临时设计稿，待评审。
> **目标：** 在 `pytest` 运行期间拦截所有对 5 个后端二进制（`clawcodex-dev` / `codex` / `hermes` / `opencode` / `dsh`）的真实调用，避免测试意外依赖外部 CLI 或网络。
> **参考：** multica `scripts/go-test-with-agent-cli-guard.sh`（把 26 个 CLI 名 shim 到 PATH，sentinel 退出码 126）。
> **不解决：** `tests/manual_e2e_*.py` 系列（设计为手动运行，本就该打真 CLI）；CI 上多后端真实 e2e（属另一份设计）。

---

## 0. 背景与现状

### 0.1 当前缺口

`orchestratord` 的 5 个后端中 3 个走 CLI（`codex` Cli 路径、`hermes` 全程、`opencode` 服务端启动 + `dsh` 启动 harness），它们在 backend 构造期或 session 创建期会 `subprocess.Popen` 真实二进制：

- `backends/orchestratord-codex/src/orchestratord_codex/backend.py:40-67` — `codex app-server --help` 探针
- `backends/orchestratord-hermes/src/orchestratord_hermes/backend.py` — 每 turn spawn `hermes` 子进程
- `backends/orchestratord-opencode/src/orchestratord_opencode/backend.py` — `opencode serve --port 0` + stderr 端口抓取
- `backends/orchestratord-dsh/src/orchestratord_dsh/backend.py` — 启动 `deepseek-harness-sdk` 子进程

虽然 backend 测试多用 SPI stub（`tests/spi_stub_helpers.py`），但仍有以下漏点：

1. **真后端 e2e 误跑**：`tests/manual_e2e_opencode_sse.py` 等虽然头部 `pytest.skip(...)`，但若有人删掉那行直接运行会真起子进程；同时本地 dev 偶尔会有人 `pytest tests/` 不带过滤 — CI 不应允许任何"我没意识到这是手测"的 pytest 进程意外敲响 5 个后端。
2. **新增后端无护栏**：新加一个走 CLI 的 backend，作者可能忘记在 `test_capability_drift.py` 注册 EXPECTED。挡板可以让"忘记注册"立刻在 CI 暴露为"无法探针"。
3. **跨平台差异**：WSL / macOS / Docker 容器里 5 个二进制未必都已装，CI 跑测的环境差异放大该问题。

### 0.2 现有积木

| 积木 | 位置 | 现状 |
|---|---|---|
| `tests/conftest.py` | 已存在 | 集中 fixture 入口，是加 PATH 重写的最佳锚点 |
| `tests/spi_stub_helpers.py` | 已存在 | 已用 `FakeBackend` 风格替代 SPI 调用，但只覆盖 SPI 层，**不挡** CLI 二进制 |
| `tests/manual_e2e_*.py` | 5 个文件 | 头部 `pytest.skip(...)` 屏蔽；是挡板的**唯一豁免**对象 |
| `[tool.pytest.ini_options]` | `pyproject.toml` | 含 `testpaths = ["tests"]`、`asyncio_mode = "auto"` |
| multica 挡板脚本 | `multica/scripts/go-test-with-agent-cli-guard.sh` | 26 个 CLI 名 → sentinels（`exit 126`）+ stderr 提示 |
| backend 描述符 | （未来 `DESIGN_two_tier_backend_registry.md` §1.2） | `BackendDescriptor.cli_command` 是单一真源；本设计的 `_backend_cli_registry` 届时被 descriptor 取代 |

### 0.3 方案总览

| # | 子方案 | 范围 | 解决 |
|---|---|---|---|
| A | 后端 CLI 清单单例 | 新增 `src/orchestratord/_backend_cli_registry.py` | 5 个二进制名集中维护，避免散落 |
| B | PATH shim 注入 | 新增 `scripts/_cli_shims/_shim_runner.py` + `scripts/install_cli_shims.py` | pytest 启动时 PATH 前置；任何 `subprocess` 调到这些名字即命中 |
| C | conftest 自动启用 | `tests/conftest.py` 加 `autouse` fixture | 不需每个测试手动 opt-in |
| D | 豁免机制 | `pytest.mark.uses_real_cli` + `manual_e2e_*.py` 头部 `pytestmark` | `manual_e2e_*` 仍可手动触发；常规测试不会触发 |

---

## 1. 方案 A — 后端 CLI 清单单例

### 1.1 目标

把 5 个后端二进制名收敛到一个 Python 单例数据源，便于：
- 挡板脚本读它生成 stub
- `test_capability_drift.py` 用它交叉验证 backend 是否对应一个真二进制
- 文档生成器用它写 README "External CLIs" 段

### 1.2 设计

新建 `src/orchestratord/_backend_cli_registry.py`：

```python
# src/orchestratord/_backend_cli_registry.py (新)
"""Single source of truth for backend CLI binary names.

每个 backend 包若需调用外部二进制，应在此注册 CLI 名。挡板
脚本（scripts/install_cli_shims.py）与 CI drift 守护都依赖
此模块，避免散落字符串。
"""

from dataclasses import dataclass

@dataclass(frozen=True)
class BackendCLI:
    """一个后端对应的外部 CLI 二进制。"""
    binary: str                # 出现在 PATH 上的命令名
    backend_package: str       # 对应的 orchestratord-* 包
    notes: str = ""            # 自由文档（探测参数、典型超时等）

KNOWN_BACKEND_CLIS: tuple[BackendCLI, ...] = (
    BackendCLI("clawcodex-dev", "orchestratord-clawcodex",
               "in-process SDK; 探针仅做 capability 探测"),
    BackendCLI("codex",          "orchestratord-codex",
               "双路径：codex app-server (SdkProcess) 或 codex exec --json (Cli)"),
    BackendCLI("hermes",         "orchestratord-hermes",
               "spawn-per-turn Cli"),
    BackendCLI("opencode",       "orchestratord-opencode",
               "opencode serve --port 0 + SSE"),
    BackendCLI("dsh",            "orchestratord-dsh",
               "deepseek-harness-sdk 子进程"),
)

def binary_names() -> tuple[str, ...]:
    return tuple(c.binary for c in KNOWN_BACKEND_CLIS)

def lookup(binary: str) -> BackendCLI | None:
    for c in KNOWN_BACKEND_CLIS:
        if c.binary == binary:
            return c
    return None
```

### 1.3 验收

- 模块导入无副作用（无 `subprocess`、无网络）
- `binary_names()` 返回 5 个字符串，与各 backend 的 `backend.py` 中的 `subprocess.Popen` 调用的第一参数一一对应

---

## 2. 方案 B — PATH shim 注入

### 2.1 目标

生成 5 个 stub 可执行，预先放到一个临时目录；`conftest.py` 把这个目录 prepend 到 `os.environ["PATH"]`。后续任何 `subprocess.Popen(["clawcodex-dev", ...])` 命中 shim。

### 2.2 shim 脚本

`scripts/_cli_shims/_shim_runner.py` 是统一入口（多 binary 共享），靠环境变量区分自身：

```python
# scripts/_cli_shims/_shim_runner.py (新)
"""挡板脚本：pytest 期间任何调用此 CLI 即失败。

stdout: 一行说明，告知测试如何豁免
stderr: 详细帮助
exit code: 126 (multica sentinel)
"""
import os, sys

def main() -> int:
    argv = sys.argv[1:]
    binary = os.environ.get("ORCHESTRATORD_GUARDED_BINARY", "<unknown>")
    pkg = os.environ.get("ORCHESTRATORD_GUARDED_BACKEND_PKG", "<unknown>")
    msg = (
        f"[backend-cli-guard] {binary!r} was invoked during pytest.\n"
        f"  argv: {argv!r}\n"
        f"  backend package: {pkg}\n"
        f"\n"
        f"  This is the default — pytest tests must NOT shell out to real agent CLIs.\n"
        f"  Use a SPI stub (tests/spi_stub_helpers.py) or mark the test with\n"
        f"  @pytest.mark.uses_real_cli and ensure the test is in tests/manual_e2e_*.py.\n"
    )
    print(msg, file=sys.stderr)
    return 126

if __name__ == "__main__":
    sys.exit(main())
```

### 2.3 安装器

`scripts/install_cli_shims.py`：在 pytest session 启动时调用，生成 5 个可执行文件到临时目录，返回目录路径。

```python
# scripts/install_cli_shims.py (新)
"""为 pytest 临时目录生成 5 个挡板可执行文件并返回路径。

conftest 调用一次，结果缓存到 os.environ["ORCHESTRATORD_CLI_GUARD_DIR"]。
"""

from __future__ import annotations
import os, stat, sys, tempfile, textwrap
from pathlib import Path

from orchestratord._backend_cli_registry import KNOWN_BACKEND_CLIS


def _render_shim_source(binary: str, backend_pkg: str, shim_runner_dir: str) -> str:
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import os, sys
        os.environ.setdefault("ORCHESTRATORD_GUARDED_BINARY", {binary!r})
        os.environ.setdefault("ORCHESTRATORD_GUARDED_BACKEND_PKG", {backend_pkg!r})
        sys.path.insert(0, {shim_runner_dir!r})
        from _shim_runner import main
        sys.exit(main())
    """)


def install_cli_shims(*, parent: Path | None = None) -> Path:
    """生成 5 个挡板可执行，返回所在目录。"""
    base = parent or Path(tempfile.mkdtemp(prefix="orchestratord-cli-guard-"))
    base.mkdir(parents=True, exist_ok=True)
    shim_runner_src = Path(__file__).resolve().parent / "_cli_shims" / "_shim_runner.py"
    (base / "_shim_runner.py").write_text(shim_runner_src.read_text())
    for cli in KNOWN_BACKEND_CLIS:
        target = base / cli.binary
        target.write_text(_render_shim_source(cli.binary, cli.backend_package, str(base)))
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return base


if __name__ == "__main__":
    d = install_cli_shims(parent=Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(d)
```

### 2.4 验收

- `python scripts/install_cli_shims.py /tmp/x && ls /tmp/x` 看到 5 个可执行 + `_shim_runner.py`
- `/tmp/x/clawcodex-dev` 执行退出码 126、stderr 含 `[backend-cli-guard]`

---

## 3. 方案 C — conftest 自动启用

### 3.1 目标

每个 pytest session 自动启用挡板，**不需**每个测试手动 opt-in。

### 3.2 改动

`tests/conftest.py` 在文件最顶部（fixture 之外）增加：

```python
# tests/conftest.py (顶部新增)
import os
from pathlib import Path

import pytest

from scripts.install_cli_shims import install_cli_shims


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "uses_real_cli: this test intentionally invokes an external "
        "backend CLI binary; the guard will be skipped for it.",
    )


@pytest.fixture(autouse=True)
def _backend_cli_guard_path(request):
    """把 5 个后端 CLI 的 stub 目录 prepend 到 PATH。

    任何 subprocess 命中这些名字立即失败（exit 126），
    除非测试被 @pytest.mark.uses_real_cli 标记，或位于
    tests/manual_e2e_*.py（自带 pytestmark）。
    """
    nodeid = request.node.nodeid
    is_exempt = (
        nodeid.startswith("tests/manual_e2e_")
        or request.node.get_closest_marker("uses_real_cli") is not None
        or os.environ.get("ORCHESTRATORD_SKIP_CLI_GUARD") == "1"
    )
    if is_exempt:
        yield
        return

    guard_dir = Path(os.environ.setdefault(
        "ORCHESTRATORD_CLI_GUARD_DIR",
        str(install_cli_shims()),
    ))
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{guard_dir}{os.pathsep}{old_path}"
    try:
        yield
    finally:
        os.environ["PATH"] = old_path
```

并在 `[tool.pytest.ini_options].pythonpath` 中加入 `scripts`（`pyproject.toml` 已设 `pythonpath = ["src"]`，需追加 `"scripts"`），让 `from scripts.install_cli_shims import install_cli_shims` 可解析。

### 3.3 验收

- `pytest tests/test_capability_drift.py -q` 通过（不调 CLI）
- 临时构造一个 `tests/_tmp_test_invokes_cli.py`：

  ```python
  import subprocess
  def test_real_cli_should_fail():
      r = subprocess.run(["clawcodex-dev", "--help"], capture_output=True)
      assert r.returncode == 126
      assert b"[backend-cli-guard]" in r.stderr
  ```

  期望：测试 PASS（即挡板成功拦截）
- 加 `ORCHESTRATORD_SKIP_CLI_GUARD=1` 环境变量时挡板完全禁用（CI 调试用）

---

## 4. 方案 D — 豁免机制

### 4.1 目标

`tests/manual_e2e_*.py` 必须能跑真 CLI；新增的 `@pytest.mark.uses_real_cli` 测试也允许。

### 4.2 改动

#### 4.2.1 标记定义

已在 §3.2 的 `pytest_configure` 中注册 `uses_real_cli` marker。

#### 4.2.2 manual_e2e 文件统一加 pytestmark

```python
# tests/manual_e2e_opencode_sse.py 顶部
import pytest
pytestmark = pytest.mark.uses_real_cli
```

（注：现有文件已 `pytest.skip(...)` 头部调用，但 marker 让"误删 skip 行"也安全）

### 4.3 验收

- `pytest tests/manual_e2e_opencode_sse.py -q` 仍然 `skip`，但**不**命中挡板（marker 跳过注入）
- `ORCHESTRATORD_SKIP_CLI_GUARD=1 pytest tests/test_x.py` 等价于挡板关闭

---

## 5. 完整改动清单

| 文件 | 改动 | 行数估计 |
|---|---|---|
| `src/orchestratord/_backend_cli_registry.py` | 新建 — CLI 名单例 | ~50 |
| `scripts/_cli_shims/_shim_runner.py` | 新建 — shim 入口 | ~30 |
| `scripts/install_cli_shims.py` | 新建 — 安装器 | ~45 |
| `tests/conftest.py` | 加 `_backend_cli_guard_path` fixture + `pytest_configure` | ~40 |
| `pyproject.toml` | `pythonpath` 追加 `"scripts"` | +1 |
| `tests/manual_e2e_opencode_sse.py` | 加 `pytestmark = pytest.mark.uses_real_cli` | +2 |
| `tests/manual_e2e_codex_as_interrupt.py` | 同上 | +2 |
| `tests/manual_e2e_codex_as_session.py` | 同上 | +2 |
| `tests/manual_e2e_clarifier.py` | 同上 | +2 |
| `tests/manual_e2e_git_sync.py` | 同上 | +2 |
| **新测试** `tests/test_backend_cli_guard.py` | 验证挡板拦截 + 豁免逻辑 | ~80 |

总计新增 3 文件 + 改 8 文件；代码约 210 行 + 测试。

---

## 6. 关键代码预览（核心 fixture 全文）

```python
# tests/conftest.py 关键新增
@pytest.fixture(autouse=True)
def _backend_cli_guard_path(request):
    nodeid = request.node.nodeid
    is_exempt = (
        nodeid.startswith("tests/manual_e2e_")
        or request.node.get_closest_marker("uses_real_cli") is not None
        or os.environ.get("ORCHESTRATORD_SKIP_CLI_GUARD") == "1"
    )
    if is_exempt:
        yield
        return

    guard_dir = Path(os.environ.setdefault(
        "ORCHESTRATORD_CLI_GUARD_DIR",
        str(install_cli_shims()),
    ))
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{guard_dir}{os.pathsep}{old_path}"
    try:
        yield
    finally:
        os.environ["PATH"] = old_path
```

---

## 7. 验收标准（Verification）

- [ ] **`pytest -q` 全套默认运行不敲真 CLI**：本地开发环境无 5 个二进制也能 `pytest tests/`
- [ ] **新增 `tests/test_backend_cli_guard.py` 全过**：
  - `test_guard_blocks_invocation` — subprocess 调 `clawcodex-dev` 返回 126 + stderr 含 sentinel
  - `test_guard_blocks_all_known_binaries` — 5 个 binary 名都拦到
  - `test_real_cli_marker_exempts` — `@pytest.mark.uses_real_cli` 测试不被注入
  - `test_manual_e2e_files_exempt_by_path` — 路径前缀匹配豁免
  - `test_skip_env_var_disables_guard` — `ORCHESTRATORD_SKIP_CLI_GUARD=1` 时 PATH 不变
- [ ] **`_backend_cli_registry` 单元测试**：覆盖 5 个 binary 唯一性、与各 backend 包内 `subprocess.Popen` 第一参数的一致性（静态扫描）
- [ ] **CI 守护**：`test_capability_drift.py` 增加一个 case：`KNOWN_BACKEND_CLIS` 与各 backend 实际使用 binary 名必须一致，否则 fail
- [ ] **跨平台**：在 Linux/macOS/Windows (Git Bash) 三种 shell 下 `subprocess.run([binary, "--help"])` 都返回 126
- [ ] **README 更新**：在"开发与测试"段加 1 段说明挡板机制 + `ORCHESTRATORD_SKIP_CLI_GUARD` 环境变量

---

## 8. 范围外（Out of Scope）

1. **`manual_e2e_*` 改造**：本设计只让它们继续能跑；不做 e2e 框架升级
2. **CI 上跑真后端 e2e**：属 `DESIGN_backend_real_e2e_ci.md`（未来设计）
3. **Windows 原生 PATH 处理**：先覆盖 POSIX；Windows 留给后续修复（PATH 在 Windows 上分隔符不同，conftest 已用 `os.pathsep`，但 shim 在 Windows 上需 `.exe` 后缀 — 这块先列 issue）
4. **每个后端的真实二进制自动发现**：若用户装了非标路径的 binary（如 `~/.local/bin/clawcodex-dev`），挡板仍优先 — 用户需用 `ORCHESTRATORD_SKIP_CLI_GUARD=1`

---

## 9. 后续（Future Work）

- 把 `_backend_cli_registry` 的内容挪进每 backend 包的 `pyproject.toml` `entry-points."orchestratord.backend_cli"`，由 core 自动聚合 — 进一步避免硬编码（与未来 `DESIGN_two_tier_backend_registry.md` 协同）
- 在 `scripts/_cli_shims/` 加 `pytest --collect-only` 模式：扫描全测试，若有未标记 `uses_real_cli` 却显式 `subprocess.Popen` 调 5 个 binary 的，立即报"未豁免"

---

## 10. 参考资料

- multica 挡板：`multica/scripts/go-test-with-agent-cli-guard.sh`（sentinel exit 126）
- multica CLI 清单：`multica/scripts/agent-cli-command-names.txt`（26 个名字）
- orchestratord 后端现状：`backends/orchestratord-*/src/orchestratord_*/backend.py`
- ADR-001 §5：[CI drift 守护](ADR-001-backends-hardening.md)