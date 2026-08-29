# orchestratord ↔ clawcodex 耦合审计

> **状态：** 临时审计稿，待评审。
> **目的：** 量化编排器核心对 clawcodex 的耦合现状，作为后续解耦工作的基线。
> **范围：** `orchestratord` 仓库 `src/orchestratord/` 与 `tests/` 两个目录，加上 `pyproject.toml` 构建配置。
> **结论先行：** 架构文档宣称"core 与后端通过 entry_points 解耦"；**实际耦合面已超出 `adapters/clawcodex.py` 边界**——7 个核心模块穿透 adapter 直接 import、12 个间接耦合、12+ 个 clawcodex 子系统被触及，主工作路径仍 100% 走 clawcodex。**M1 边界事实上已被打破**。

---

## 1. 总体盘点

| 维度 | 数量 | 严重度 |
|---|---|---|
| 直接 `import extensions.*` 的核心模块 | **1**（`adapters/clawcodex.py`） | 受控 |
| **穿透 adapter 直接 import `extensions.*` 或 `from ..api.query` 的核心模块** | **7** | **P0 — 边界已破坏** |
| 通过 `orchestratord.adapters.clawcodex` 间接耦合的核心模块 | **12** | 严重 |
| 该 adapter re-export 的 clawcodex 类型/函数 | **37** | — |
| 引入 clawcodex 顶层子模块数 | **8 + lkb = 9** | 严重 |
| pythonpath 硬编码绝对路径 | **2**（`clawcodex`、`clawcodex/extensions/lkb/src`） | 严重 |
| 触及的 clawcodex 子系统面 | **≥ 12**（api、orchestrator_runtime、capabilities、agent_dashboard、lkb、orchestrator 等） | 严重 |
| Symphony 项目命名遗留 | **6 处** | 中 |
| 外部服务硬编码默认端点 | **5 类**（DeepSeek / Linear / GitHub / Gitee / GitCode） | 低 |
| 主动断言 clawcodex 为默认实现的测试 | **≥ 4 个文件** | 严重 |
| 读取 `~/.clawcodex/...` 路径的测试 | **≥ 1**（`test_orchestrator_f49_transcript.py`） | 严重 |
| 通过 `extensions.capabilities.*` 直接断言结构存在的测试 | **6 个 Protocol 类** | 严重 |

---

## 2. 直接耦合入口 — `adapters/clawcodex.py`

`src/orchestratord/adapters/clawcodex.py:1-13` 的 docstring 自陈：

> "This is the **only** module in orchestratord core that imports from `extensions.*`. All other orchestratord modules import clawcodex types through this adapter, keeping the clawcodex coupling surface to a single file."

> "M1 target: after this adapter is in place, a CI static check will enforce that no other `src/orchestratord/**/*.py` file imports from `extensions.*`."

> "In M3+ this adapter will be extracted into the `orchestratord-clawcodex` backend package and registered via entry_points."

文件实际从以下 8 个 clawcodex 子模块导入并把它们 re-export 给核心：

| 子模块 | 行 | 暴露符号 |
|---|---|---|
| `extensions.api.query` | `:17` | `PhaseComplete, QueryConfig, QueryRunner, SessionComplete, TextDelta, ToolCallEvent, ToolResultEvent, TurnComplete` |
| `extensions.orchestrator_runtime.adapters.clawcodex_compat` | `:29` | `CardUpdateCapability, ChannelCapability, CommandRouter, ControlBridge, InboundMessage, MessageSemantics, RateLimitError, ToolContext, _run_git, get_current_branch, get_default_branch, get_file_status, get_repo_root, is_rate_limit_error, task_v2_guidelines` |
| `extensions.orchestrator_runtime.utils.messages_impl` | `:48` | `TextBlock, ToolResultBlock, ToolUseBlock, create_assistant_message, create_user_message, message_from_dict` |
| `extensions.orchestrator_runtime.adapters` | `:58` | `build_default_agent_runtime, build_default_bootstrap_state, build_default_coordinator_provider, build_default_session_storage` |
| `extensions.capabilities.recorder` | `:66` | `AsciicastCapture` |
| `extensions.capabilities.automation_state_protocol` | `:69` | `AutomationStateObserver, AutomationStateReporter` |
| `extensions.api.orchestration` | `:75` | `OrchestrationSubsystem` |
| `extensions.agent_dashboard` + `.sources.orchestrator_source` | `:78-79` | `register_dashboard_source, OrchestratorDashboardSource` |
| **合计** | | **37 个符号** |

`src/orchestratord/adapters/__init__.py:1-4` 同步声明：

```python
"""Adapters package — clawcodex compatibility boundary.
All ``extensions.*`` imports are confined to ``.clawcodex``.
Other modules import through this package.
"""
```

**审计判定：M1 目标已被打破**——`adapters/clawcodex.py` 自陈是"唯一直接 import 入口"，但**还有 7 个核心模块绕过 adapter 直接 import clawcodex 内部模块**（详见 §3.6）。该 adapter 实际上既不是"唯一入口"也不是"被 CI 强制"——grep 即可证明。

---

## 3. 间接耦合面 — 12 个核心模块经 `adapters.clawcodex` 引用 clawcodex

按耦合深度排序：

### 3.1 极重度耦合 — `agent_runner.py`（12 处引用）

`src/orchestratord/agent_runner.py` 是整个 orchestrator 的**核心执行单元**。

| 行 | 内容 | 性质 |
|---|---|---|
| `:3` | docstring："Port of Symphony's AgentRunner, replacing Codex JSON-RPC with QueryRunner." | 自陈身份 |
| `:20` | `from orchestratord.adapters.clawcodex import PhaseComplete, QueryConfig, QueryRunner` | 主类直接消费 clawcodex |
| `:21` | `from … import SessionComplete, TextDelta, ToolCallEvent, ToolResultEvent, TurnComplete` | 全部 clawcodex 原生事件 |
| `:27` | `from … import get_file_status` | 文件系统工具 |
| `:38-41` | `from … import RateLimitError, is_rate_limit_error` | 错误类型 |
| `:332, 498, 542, 704, 1083, 1760, 2021, 2094` | 多个函数内部再次 `from orchestratord.adapters.clawcodex import …` | 重复 import |
| `:439` | 类 `AgentRunner` docstring："Execute a single issue via ClawCodex QueryRunner." | 自陈 |
| `:494` | 注释："`extensions.orchestrator.agent_runner.QueryRunner` directly." | 自陈 |
| `:543-548` | 翻译器里直接 `isinstance(event, TextDelta / ToolCallEvent / …)` | 类型耦合 |
| `:1786` | `query_config = QueryConfig(...)` | 构造 clawcodex 对象 |
| `:1832` | `runner = QueryRunner(query_config)` | **直接实例化 clawcodex QueryRunner** |
| `:1890-2696` | 整个 stream 消费循环里反复出现 `TextDelta / ToolCallEvent / ToolResultEvent / SessionComplete / TurnComplete / PhaseComplete` | 全面耦合 |

**判定：** `AgentRunner` **完全没有走 SPI**。它直接构造 `QueryConfig`/`QueryRunner`，消费 clawcodex 原生事件类，没有 `backend.create_session(spec)` 调用。这是 orchestrator 对 clawcodex 的**主调用路径**。

### 3.2 重度耦合 — `orchestrator.py`（3 处块）

| 行 | 内容 |
|---|---|
| `:17-21` | `from orchestratord.adapters.clawcodex import (CardUpdateCapability, ChannelCapability, ToolContext)` |
| `:59-64` | `from orchestratord.adapters.clawcodex import (_run_git, get_default_branch, get_file_status, get_repo_root)` |

且 `:23` `from .agent_runner import AgentRunner, AgentSession, RetryItem` —— 由于 `agent_runner` 模块自身深度耦合 clawcodex，**任何 import orchestrator 的代码路径都间接 import clawcodex**。

`:192, 201-205` 的注释承认这一点：

```python
# a BackendRunner so the orchestrator can drive it through the same
# ``run()`` interface it uses for AgentRunner.  When backend is None
# (the default), the existing AgentRunner path is used unchanged.
```

主调用点 `:2147, 3081`：`self.agent_runner.run(...)` —— 即走的是 `AgentRunner.run()` → clawcodex QueryRunner。

### 3.3 中度耦合 — 8 个 sink/client 模块

| 文件 | 行 | 引用的 clawcodex 符号 | 业务含义 |
|---|---|---|---|
| `progress_sink.py` | `:31` | `PhaseComplete, SessionComplete, TurnComplete` | 进度上报 |
| `progress_reporter.py` | `:22` | 同上 | 同上 |
| `feishu_activity_sink.py` | `:31-32` | 同上 | 飞书活动 sink |
| `im_gateway_client.py` | `:31, :35` | `CommandRouter, ControlBridge` 等 + 重复 import | IM 网关 |
| `asciicast_sink.py` | `:26-27` | `AsciicastCapture` | asciicast 录制 |
| `git_sync.py` | `:14` | clawcodex_compat helpers | Git 同步 |
| `backend_runner.py` | `:397` | `get_file_status` | 即使走 SPI 路径仍需 clawcodex |
| `prompt_builder.py` | `:16` | `task_v2_guidelines` | 提示词构造 |
| `cli/server.py` | `:1147, 1155-1156` | `OrchestrationSubsystem, register_dashboard_source, OrchestratorDashboardSource` | CLI 启动 dashboard |

### 3.4 隐式耦合 — `session_state.py`

`src/orchestratord/session_state.py:110-113`：

```python
# _save_json_snapshot is monkey-patched by agent_runner.py at
# module-load time, because it imports from orchestratord.adapters.clawcodex
# and src.bootstrap.state which require the full clawcodex environment.
_save_json_snapshot: Any = None  # type: ignore[assignment]
```

`_save_json_snapshot` 默认是 `None`，被 `agent_runner.py` 在模块加载期运行时 monkey-patch 进来。**这意味着 `session_state` 在没有 clawcodex 时是无法正常工作的空 stub**。表面是软依赖，本质仍是硬依赖。

### 3.5 间接耦合 — `cli/server.py`

启动 dashboard 时（`cli/server.py:1147-1156`）通过 adapter 引入 `OrchestrationSubsystem` 与 `register_dashboard_source`/`OrchestratorDashboardSource`，并把 `OrchestratorDashboardSource(OrchestrationSubsystem(...))` 注册到全局 dashboard 存储。这意味着**即使 orchestrator 走 SPI backend 路径，只要启动 CLI 的 dashboard，仍需要 clawcodex 装配**。

### 3.6 穿透耦合 — M1 边界已破坏

`adapters/clawcodex.py:8-10` 声称"M1 目标：CI 静态检查强制 `src/orchestratord/**` 其他文件不能从 `extensions.*` 直接 import"。**该承诺未兑现**——下列 7 处 import 都在 `adapters/clawcodex.py` **之外**，且通过 `from ..api.query` 等相对路径绕过 grep 静态检查。

`/mnt/c/WorkSpace/orchestratord/src/orchestratord/api/` 在仓库内**不存在**（用 `ls` 确认），所以 `from ..api.query import Y` 实际通过 `pyproject.toml:55-58` 注入的 pythonpath 解析到 `extensions/api/query.py`——**与直接 `from extensions.api.query import Y` 等效**。

| 文件 | 行 | import 形式 | 引用的 clawcodex 符号 |
|---|---|---|---|
| `agent_runner.py` | `:3085` | `from extensions.orchestrator_runtime.utils.messages_impl import message_from_dict` | 路径写成绝对，直接穿透 |
| `agent_runner.py` | `:44-45` (TYPE_CHECKING) | `from ..capabilities.agent_protocol import AgentLoopProtocol` / `..capabilities.event_protocol import ToolEventProtocol` | 协议类，类型检查期需要 |
| `status_dashboard.py` | `:180` | `from ..api.query import TextDelta, ToolCallEvent, ToolResultEvent` | clawcodex 事件类 |
| `events/emitter.py` | `:19` | `from ..api.query import SessionComplete, PhaseComplete, TurnComplete` | 终态事件类 |
| `state_journal_sink.py` | `:20` | `from ..api.query import PhaseComplete, SessionComplete, TurnComplete` | 终态事件类 |
| `workflow_engine/observability.py` | `:253` | `from ...api.query import PhaseComplete` | 阶段事件类 |
| `workflow_engine/observability.py` | `:273` | `from ...api.query import SessionComplete` | 终态事件类 |

**风险等级：P0**——这些 import 同样依赖 `pyproject.toml:55-58` 的绝对路径，在 CI runner / 新开发者机器 / 容器中**全部会失败**，与 `adapters/clawcodex.py` 是同一根因，但被相对路径形式掩盖。

**审计判定**：adapter 自陈的"M1 唯一边界"**已不成立**。修复时应一并治理：

1. 把这些 import 全部迁回 `adapters/clawcodex.py`，由 adapter 统一 re-export
2. 或把 `adapters/clawcodex.py` 整体移到 `backends/orchestratord-clawcodex` 包内，按 M3+ 路线彻底消除
3. CI grep 应同时匹配 `extensions.` 和 `from \.\.+api\.` 两种模式

### 3.7 lkb 专项耦合

`pyproject.toml:58` 直接把 `/mnt/c/WorkSpace/clawcodex/extensions/lkb/src` 加入 pythonpath——这是 clawcodex "task V2" 业务模块的 Python 源根，**与 `extensions/` 主干不同源**。

| 处 | 内容 |
|---|---|
| `pyproject.toml:58` | `"/mnt/c/WorkSpace/clawcodex/extensions/lkb/src"` |
| `prompt_builder.py:16` | `from orchestratord.adapters.clawcodex import task_v2_guidelines`（经 adapter 间接） |
| `prompt_builder.py:293-295` | `lkb_guidance = task_v2_guidelines()` → 渲染进 prompt |

**审计判定**：lkb 表面是经 adapter 引入，**但 `task_v2_guidelines` 函数名本身即 lkb 业务术语**，且需要单独的 `lkb/src` pythonpath 才能解析。即使 §3.6 的穿透全部修好，**lkb 仍是独立于 `extensions.*` 主干的耦合面**，必须单独治理。

### 3.8 clawcodex 子系统面

`/mnt/c/WorkSpace/clawcodex/extensions/` 下有 **20+ 个子系统**（`agent`、`agent_dashboard`、`agents`、`api`、`capabilities`、`context_providers`、`daemon`、`im_gateway`、`lkb`、`orchestrator`、`orchestrator_runtime`、`permissions`、`ports`、`prompt_lab`、`providers_ext`、`recording`、`remote_api`、`skills_ext`、`sop_converter`、`tool_system_ext`、`trae`、`visualizer`），orchestratord 核心触及其中至少 **12 个**：

| 子系统 | 触及处 |
|---|---|
| `extensions.api.query` | adapter + 7 处穿透（§3.6） |
| `extensions.api.orchestration` | adapter（OrchestrationSubsystem） |
| `extensions.orchestrator_runtime.utils.messages_impl` | adapter + `agent_runner.py:3085` |
| `extensions.orchestrator_runtime.adapters.clawcodex_compat` | adapter |
| `extensions.orchestrator_runtime.adapters` | adapter |
| `extensions.capabilities.recorder` | adapter（AsciicastCapture） |
| `extensions.capabilities.automation_state_protocol` | adapter |
| `extensions.capabilities.agent_protocol` | TYPE_CHECKING + tests |
| `extensions.capabilities.event_protocol` | TYPE_CHECKING + tests |
| `extensions.capabilities.{tool,context,provider,headless}_protocol` | tests（`test_layer_isolation.py`） |
| `extensions.capabilities.headless_runner` | tests |
| `extensions.agent_dashboard` + `.sources.orchestrator_source` | adapter |
| `extensions.lkb.*` | pyproject + `task_v2_guidelines`（§3.7） |

**审计判定**：orchestratord 不是"与一个 clawcodex 后端对接"，而是**与 clawcodex 整个 extensions 树对接**。任何 clawcodex 内部重构都会传导到这里——这是文档与现实的最大差距。

### 3.9 Symphony 项目命名遗留

orchestrator 是 Symphony 的 Python 移植版（`orchestrator.py:1` docstring 自陈 "Port of Symphony's Orchestrator"）。**移植没做完，命名没统一**：

| 处 | 内容 |
|---|---|
| `src/orchestratord/orchestrator.py:1` | docstring："Port of Symphony's Orchestrator" |
| `src/orchestratord/agent_runner.py:3` | docstring："Port of Symphony's AgentRunner, replacing Codex JSON-RPC with QueryRunner" |
| `src/orchestratord/config/schema.py:423` | `default_workspace_root()` 返回 `os.path.join(os.environ.get("TMPDIR", "/tmp"), "symphony_workspaces")` |
| `src/orchestratord/cli/workflow.py:206` | CLI 默认值 `default="/tmp/symphony_workspaces/myproject"` |
| `src/orchestratord/cli/workflow.py:327` | `ws_root = val(args.workspace_root, "Workspace root", "/tmp/symphony_workspaces/myproject")` |
| `src/orchestratord/templates/workflow.template.md:72` | 注释示例 `e.g. /tmp/symphony_workspaces/myrepo` |
| `tests/test_orchestrator_workspace_locator.py:81` | 测试 fixture 路径含 `symphony_workspaces` |

**审计判定**：6 处硬编码路径 / 注释让用户在产线见到 `symphony_workspaces` 字样，**会留下"这是 Symphony"的错觉**。属于产品级耦合（命名一致性），优先级低于代码耦合但影响品牌识别。

### 3.10 外部服务默认端点

下列默认值都是**可通过环境变量 / 配置文件覆盖**的，因此不算硬编码，但展示了"orchestratord 默认走哪些 SaaS / API"的隐含假设：

| 服务 | 文件 / 行 | 默认端点 |
|---|---|---|
| DeepSeek API | `config/schema.py:186, 833`；`mode_router.py:255` | `https://api.deepseek.com/chat/completions` |
| Linear API | `config/schema.py:434`；`linear/adapter.py:17`；`linear/client.py:16`；`tracker_kinds.py:47` | `https://api.linear.app/graphql` |
| GitHub | `repo_tracker/client.py:69, 74`；`tracker_kinds.py:56-57` | `https://api.github.com`、`https://github.com` |
| Gitee | `repo_tracker/client.py:78, 83`；`tracker_kinds.py:67-68`；`cli/workflow.py:369` | `https://gitee.com/api/v5` |
| GitCode | `repo_tracker/client.py:87, 93`；`tracker_kinds.py:78-79`；`cli/workflow.py:368` | `https://api.gitcode.com/api/v5`、`https://gitcode.com` |

**审计判定**：优先级低。`mode_router.py:255` 默认 DeepSeek 端点与"backend-agnostic"叙事存在张力——一个号称与 backend 解耦的 orchestrator，在 LLM 路由层默认 DeepSeek。这点可以放在产品级 ADR 里讨论。

---

## 4. 构建/部署耦合 — `pyproject.toml`

`pyproject.toml:55-58`：

```toml
[tool.pytest.ini_options]
pythonpath = [
    "src",
    "/mnt/c/WorkSpace/clawcodex",                      ←绝对路径
    "/mnt/c/WorkSpace/clawcodex/extensions/lkb/src",  ←绝对路径
]
```

**后果：**
- 任何不在该开发机器上的环境（CI runner、容器、新开发者、产线）一旦路径不符，`pytest` 直接 `ImportError`。
- 没有 `package_dir` 或 `setuptools` 钩子暴露这种依赖，等价于"软链接契约"。
- 与"core 通过 entry_points 解耦"的承诺**正面冲突**。

---

## 5. 测试层耦合

测试不仅在使用 clawcodex，还在**主动断言 clawcodex 是默认实现**——把当前耦合当 invariant 固化。

### 5.1 主动断言默认实现为 clawcodex — `test_agent_runner_protocol_injection.py`

`tests/test_agent_runner_protocol_injection.py:124-138`：

```python
"""A default-constructed runner resolves Clawcodex adapters on demand."""
# Default implementations come from Clawcodex factories.
assert type(runner._agent_runtime).__name__ == "ClawcodexAgentRuntime"
assert type(runner._session_storage).__name__ == "ClawcodexSessionStorage"
assert type(runner._coordinator).__name__ == "ClawcodexCoordinatorProvider"
assert type(runner._bootstrap_state).__name__ == "ClawcodexBootstrapState"
```

**判定：反向断言才对**。真正的 backend-agnostic 测试应当断言"默认构造不依赖任何具体后端"，clawcodex 是注入的可选特例。

### 5.2 把 clawcodex 当上游引用 — `test_layer_isolation.py`

`tests/test_layer_isolation.py:1-191`：

- `:3-8`："See: docs/UPSTREAM_SYNC_DESIGN-decoupling.md Section 4.2"
- `:33-34`：要求 `CLAWCODEX_CI=1` 环境变量才跳过
- `:58, 67, 77, 83, 90, 102-103`：`from extensions.capabilities.{agent,tool,context,provider,event,headless}_protocol import …`
- 直接断言 6 个 `extensions.capabilities.*` Protocol 类的属性存在

这意味着测试把 clawcodex 当作**事实上的上游**——orchestratord 在它眼里不是独立产品，而是 clawcodex 的下游补丁。

### 5.3 硬编码 clawcodex 存储路径 — `test_orchestrator_f49_transcript.py`

- `:3`："The transcript subcommand reads `~/.clawcodex/sessions/{run_id}/transcript.jsonl`"
- `:208, 243`：`monkeypatch.setattr(..., "clawcodex_ext.services.session_storage.SESSIONS_DIR", ...)`

把 clawcodex 内部模块路径当作 fixture 接口。

### 5.4 IM 协议注入测试 — `test_im_channel_protocol_injection.py`

- `:52`："clawcodex_compat shims (CommandRouter / ControlBridge)"
- `:97`："importing clawcodex_ext at call time"

测试的是 clawcodex_compat 这一 shim 层本身——一旦移除 shim 这些测试就无意义。

### 5.5 E2E 测试的 clawcodex 假设 — `manual_e2e_f38.py`

- `:160, 304`："## ClawCodex Run Summary"
- `:235, 371, 515`：分支前缀 `clawcodex/issue-e2e-...`
- `:245`：`/ ".clawcodex"` 工作目录
- `:381`：`assertNotIn("branch_name: clawcodex/", md)`

整个 E2E 测试集基于"orchestratord = clawcodex 的 orchestrator 子项目"假设编写。

---

## 6. 与既定迁移计划的对照

`backends/orchestratord-clawcodex/src/orchestratord_clawcodex/backend.py:6-13` 的 strangler-fig 计划：

```
Phase B (current): this backend wraps QueryRunner directly
Phase C: freezes as shim, forwarding to orchestratord
Phase D: deleted, replaced by entry_points registration
```

**当前实际进度：**

| 阶段 | 计划要求 | 实际状态 | 差距 |
|---|---|---|---|
| **Phase B** | backend 自包含地包装 QueryRunner；core 不动 | 部分完成：backend 包已存在，但 `agent_runner.py` 仍**直接**调用 `QueryRunner`，绕开 backend 包；§3.6 列举的 7 处穿透也未治理 | **未达 Phase B 完成标准** |
| **Phase C** | backend 变成 shim，转发到 orchestratord | 未开始 | — |
| **Phase D** | 删除 backend 包，由 entry_points 替代 | 未开始 | — |

`adapters/clawcodex.py:8-10` 又提到与 Phase B 平行的"M1 → M3+" 路线图：

```
M1: adapter 在位 + CI 静态检查
M3+: adapter 抽到 orchestratord-clawcodex 包内
```

M1 **已被破坏**（adapter 存在但**不是唯一入口**；§3.6 列举的 7 处穿透证明边界失效），CI 静态检查**未实际落地**——下文 §8 第 1 项给出快速写一个的方案，且静态检查应同时匹配 `extensions.` 与 `from \.\.+api\.` 两种模式。

---

## 7. CI 静态检查缺口

`adapters/clawcodex.py:8-10` 承诺"M1 目标：CI 静态检查强制其他核心模块不能 `import extensions.*`"，但目前仓库**没有对应的 CI 步骤**。

更糟的是，§3.6 列举的 7 处穿透**不在 `extensions.` 字符串匹配范围内**——它们走的是 `from ..api.query` 这种相对形式——所以即使写一个简单的 grep 也会漏掉。

**这是最容易补上的一个缺口**，同时也是必须修的（边界已破）。见 §8 第 1 项。

---

## 8. 风险分级与建议路径

### 8.1 风险等级

| 风险 | 等级 | 触发条件 |
|---|---|---|
| 新开发者/CI 无法跑测试 | **P0** | 不在 dev 的 WSL 路径上 |
| `session_state._save_json_snapshot` 是 None 时静默失败 | **P0** | clawcodex 未安装 |
| **穿透耦合（§3.6）的 7 处 import 在任何非 dev 环境静默 ImportError** | **P0** | CI runner / 新机器 |
| orchestrator dashboard 启动崩溃 | **P1** | clawcodex 未装且启 dashboard |
| 任何走 `BackendRunner`（非默认）的用户仍需 clawcodex 文件系统辅助 | **P1** | — |
| 替换 AgentRunner 主路径时缺少回归保护 | **P1** | 解耦重构 |
| 测试断言 `ClawcodexAgentRuntime` 等作为默认实现 | **P2** | 解耦重构时反向断言改写 |
| Symphony 命名导致产品识别混乱 | **P2** | 产线文档、issue tracker |
| `mode_router.py` 默认 DeepSeek 端点与"backend-agnostic"叙事冲突 | **P2** | 跨服务商迁移 |

### 8.2 解耦路线（高层）

> 详细里程碑文档不在本文档范围内；下面给出关键节点。

1. **立刻可做（P0）**
   - `pyproject.toml` 移除 `/mnt/c/WorkSpace/clawcodex` 与 `/mnt/c/WorkSpace/clawcodex/extensions/lkb/src` 硬编码；改为相对路径或独立 `clawcodex_ext` pip 依赖
   - 加 CI 静态检查脚本（**同时匹配两种穿透模式**）：

```bash
# 主匹配：直接 extensions import
grep -rE '^\s*(from|import)\s+extensions(\.|\s|$)' \
    src/orchestratord --include='*.py' \
    | grep -v '^src/orchestratord/adapters/clawcodex.py' \
    && exit 1

# 副匹配：相对路径形式的穿透
grep -rE '^\s*from\s+\.\.+api(\.|\s|$)' \
    src/orchestratord --include='*.py' \
    && exit 1

# 测试层匹配：测试文件不能 from extensions
grep -rE '^\s*(from|import)\s+extensions(\.|\s|$)' \
    tests/ --include='*.py' \
    | grep -v 'test_layer_isolation' \
    && exit 1

exit 0
```

2. **短期（P1）**
   - `agent_runner.py` 重构：把 `QueryRunner(query_config)` 调用替换为 `BackendRunner` + `backend.create_session(spec)`；把 `TextDelta/ToolCallEvent/…` isinstance 树替换为 SPI `EventKind` 树
   - §3.6 列举的 7 处穿透 import 全部迁回 `adapters/clawcodex`（由 adapter 统一 re-export）
   - `backend_runner.py:397` 的 `get_file_status` 改成 orchestrator 自有 git helper（不依赖 clawcodex_compat）

3. **中期（P1）**
   - 12 个核心模块的 clawcodex 类型引用全部换成 SPI 类型或新协议
   - `session_state._save_json_snapshot` 改为正经依赖注入，删除 monkey-patch
   - §3.7 lkb 专项：把 `task_v2_guidelines` 调用挪到 `orchestratord-clawcodex` 后端包内（注入而非直接调用）

4. **长期（P2）**
   - `OrchestrationSubsystem, register_dashboard_source, AsciicastCapture, task_v2_guidelines` 等 clawcodex 原生抽象下沉到 `orchestratord-clawcodex` 包内，或抽象为 orchestrator 侧的可插拔接口
   - §3.8 列出的 12+ 个 clawcodex 子系统面：分类成"必须经 backend / 可以下沉到 backend / 必须留在 core"三类
   - §3.9 Symphony 命名清理：把 6 处硬编码/注释/docstring 中的 `Symphony` / `symphony_workspaces` 替换成 `orchestratord` / `orchestratord_workspaces`
   - §3.10 端点默认值的 `mode_router.py:255` DeepSeek 默认值移到配置层，不在代码层硬编码
   - 测试断言反向：默认构造应是 generic SPI，clawcodex 是注入的可选特例
   - 删除 `tests/test_layer_isolation.py` 中对 `extensions.capabilities.*` 的直接引用；改在 `orchestratord-clawcodex` 包内测试

---

## 9. 验收标准（用于判断解耦完成）

1. ✅ `pip install orchestratord` 在空环境成功，**不**要求 `/mnt/c/WorkSpace/clawcodex` 路径存在
2. ✅ `pytest` 在 CI 上跑通，**不**要求 `CLAWCODEX_CI` 环境变量
3. ✅ 核心代码 `grep -rE 'extensions(\.|\s)' src/orchestratord/` 命中数 = 0（含 `adapters/clawcodex.py` 移入 `orchestratord-clawcodex` 包内后的最终状态）
4. ✅ §3.6 列出的 7 处穿透 import 全部归零；`grep -rE 'from \.\.+api(\.|\s|$)' src/orchestratord/` 命中 = 0
5. ✅ `tests/` 目录中 `grep -rE 'extensions(\.|\s)' tests/ --include='*.py' | grep -v test_layer_isolation` 命中 = 0
6. ✅ `tests/test_agent_runner_protocol_injection.py` 中"默认实现 = Clawcodex*"的断言已替换为反向断言
7. ✅ `OrchestrationSubsystem` / `register_dashboard_source` / `AsciicastCapture` / `task_v2_guidelines` 等抽象下沉或替换
8. ✅ orchestrator 的 `run()` 主路径完全走 SPI：`backend.create_session(spec)` 而非 `QueryRunner(query_config)`
9. ✅ `grep -rE 'symphony' src/orchestratord/ tests/` 命中仅限注释/历史说明，无任何用户可见默认值

满足 1–5 后，orchestratord 在工程意义上达到 `backends/*` 与 `src/` 解耦；满足 6–8 后，在语义与测试覆盖上也达到解耦；满足 9 后，产品命名一致。

---

## 附录 A：耦合点快速查询表

```bash
# (1) 直接 extensions.* import —— 应当返回 0 行（adapter 移入后端包后）
$ grep -rE '^\s*(from|import)\s+extensions(\.|\s|$)' \
    src/orchestratord/ tests/ --include='*.py'

# (2) 相对路径形式的穿透 —— 应当返回 0 行
$ grep -rE '^\s*from\s+\.\.+api(\.|\s|$)' \
    src/orchestratord/ --include='*.py'

# (3) Symphony 命名遗留 —— 应当返回 0 行（仅允许注释/历史说明）
$ grep -rE 'symphony' src/orchestratord/ tests/ --include='*.py' --include='*.md'

# (4) 绝对路径 pythonpath —— 应当返回 0 行
$ grep -E '/mnt/c/WorkSpace/clawcodex' pyproject.toml
```

**全部命中为 0 即视为解耦完成**。这是解耦完成的**机械判据**。

## 附录 B：被本文档审查过的文件清单

```
src/orchestratord/
├── adapters/
│   ├── __init__.py                [审] §2
│   └── clawcodex.py               [审] §2
├── agent_runner.py                [审] §3.1, §3.6, §3.9
├── orchestrator.py                [审] §3.2, §3.9
├── backend_runner.py              [审] §3.3
├── git_sync.py                    [审] §3.3
├── progress_sink.py               [审] §3.3
├── progress_reporter.py           [审] §3.3
├── feishu_activity_sink.py        [审] §3.3
├── im_gateway_client.py           [审] §3.3
├── asciicast_sink.py              [审] §3.3
├── prompt_builder.py              [审] §3.3, §3.7
├── session_state.py               [审] §3.4
├── cli/server.py                  [审] §3.5
├── cli/workflow.py                [审] §3.9
├── events/emitter.py              [审] §3.6
├── state_journal_sink.py          [审] §3.6
├── status_dashboard.py            [审] §3.6
├── workflow_engine/observability.py [审] §3.6
├── config/schema.py               [审] §3.9, §3.10
├── mode_router.py                 [审] §3.10
├── linear/adapter.py              [审] §3.10
├── linear/client.py               [审] §3.10
├── repo_tracker/client.py         [审] §3.10
├── tracker_kinds.py               [审] §3.10
└── templates/workflow.template.md [审] §3.9

pyproject.toml                     [审] §4, §3.7

tests/
├── test_agent_runner_protocol_injection.py    [审] §5.1
├── test_layer_isolation.py                    [审] §5.2
├── test_orchestrator_f49_transcript.py        [审] §5.3
├── test_im_channel_protocol_injection.py      [审] §5.4
├── manual_e2e_f38.py                          [审] §5.5
└── test_orchestrator_workspace_locator.py     [审] §3.9

backends/orchestratord-clawcodex/
└── src/orchestratord_clawcodex/backend.py     [审] §6

外部耦合面（clawcodex 仓库结构）：
├── extensions/api/{query,query_middleware,orchestration}.py     [审] §3.6, §3.8
├── extensions/api/                                              [审] §2
├── extensions/orchestrator_runtime/utils/messages_impl.py       [审] §2, §3.6
├── extensions/orchestrator_runtime/adapters/clawcodex_compat.py [审] §2
├── extensions/orchestrator_runtime/adapters/                    [审] §2
├── extensions/capabilities/{recorder,automation_state_protocol}.py [审] §2
├── extensions/capabilities/{agent,tool,event,context,provider,headless}_protocol.py  [审] §3.6, §5.2
├── extensions/capabilities/headless_runner.py                   [审] §5.2
├── extensions/agent_dashboard{,/sources/orchestrator_source.py}  [审] §2
└── extensions/lkb/                                              [审] §3.7
```