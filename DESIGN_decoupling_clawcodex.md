# Design: Orchestratord 与 Clawcodex 全面解耦

**状态:** 草案
**日期:** 2026-08-27
**目标:** 移除 orchestratord 核心对所有 clawcodex 包的硬依赖，使 clawcodex 降级为"只是其中一个 agent backend"

---

## 1. 当前耦合全景

`src/orchestratord/` 中对 clawcodex 的引用分为以下类别：

| 类别 | 涉及文件 | 依赖的 clawcodex 模块 | 行数估计 |
|------|---------|----------------------|---------|
| A. Agent 后端 | `agent_runner.py`, `adapters/clawcodex.py`, `backend_runner.py` | `extensions.api.query`, `extensions.orchestrator_runtime.*`, `orchestratord_clawcodex.backend` | ~80 |
| B. IM 网关/消息通道 | `im_gateway_client.py`, `channel_sink.py`, `feishu_activity_sink.py`, `cli/server.py` | `clawcodex_ext.services.im_gateway.*`, `clawcodex_ext.messaging.*`, `clawcodex_ext.services.channels.*` | ~20 |
| C. Session 存储/恢复 | `cli/resume_session.py`, `cli/takeover.py`, `cli/issue.py` | `clawcodex_ext.agent.session`, `clawcodex_ext.services.session_storage`, `clawcodex_ext.services.session_resume` | ~8 |
| D. 工具系统集成 | `progress_sink.py`, `progress_reporter.py` | `clawcodex_ext.tool_system.context`, `clawcodex_ext.tool_system.tools.*` | ~6 |
| E. Provider/模型运行时 | `orchestrator.py` | `clawcodex_ext.providers.runtime` | ~2 |
| F. Issue 澄清 | `orchestrator.py`, `issue_clarifier/` | `clawcodex_ext.providers.runtime` (同上) | ~2 |
| G. Intent Forecast | `orchestrator.py` | `clawcodex_ext.intent_forecast.focus` | ~1 |
| H. Task v2 规范 | `prompt_builder.py` | `task_v2_guidelines` (via adapter) | ~1 |
| I. Agent 展开 | `prompt_builder.py` | `clawcodex_ext.agent.load_agents_dir`, `clawcodex_ext.command_system.input_processing` | ~2 |
| J. 任务注册表 | `agent_runner.py` | `clawcodex_ext.task_registry` | ~1 |
| K. 诊断 | `cli/server.py` | `clawcodex_ext.diagnostics` | ~1 |
| L. Dashboard 集成 | `cli/server.py` | `extensions.agent_dashboard`, `extensions.api.orchestration` | ~3 |
| M. 入口点 | `im_gateway_client.py` | `clawcodex_ext.entrypoints.orchestrator` | ~1 |
| N. 遗留路径 | `workspace_locator.py`, `report_writer.py`, `modes/pipeline.py`, 等多处 | `~/.clawcodex/` 路径引用 | ~30 |

---

## 2. 解耦策略

### 2.1 保留并自建：IM 网关/消息通道（类别 B）

**决策：** 参考 clawcodex 实现，自建独立的 IPC 协议和消息模型，零 clawcodex 依赖。

**参考 clawcodex 源码位置**（`/mnt/c/WorkSpace/clawcodex/extensions/`）：

| 参考文件 | 内容 | 自建替代 |
|---------|------|---------|
| `clawcodex_ext/services/im_gateway/ipc_client.py` | `GatewayIpcClient` — UDS IPC 客户端 | `ipc/client.py` |
| `clawcodex_ext/services/im_gateway/ipc_protocol.py` | `GatewayFrame` — 帧格式定义 | `ipc/protocol.py` |
| `clawcodex_ext/services/im_gateway/models.py` | `OutboundMessage`, `IM_DIRECT_ALL_ORIGIN` | `ipc/models.py` |
| `clawcodex_ext/messaging/semantics.py` | `MessageClassifier` — 消息语义分类 | `ipc/models.py`（内联） |
| `clawcodex_ext/services/channels/capabilities.py` | `CardUpdateCapability` — 飞书卡片 | `feishu_activity_sink.py`（直接调飞书 API） |
| `extensions/orchestrator_runtime/adapters/clawcodex_compat.py` | `InboundMessage`, `MessageSemantics`, `CommandRouter`, `ControlBridge` | `ipc/models.py`（内联） |

**当前依赖：**
- `GatewayIpcClient` — UDS IPC 客户端
- `OutboundMessage` / `InboundMessage` — 消息模型
- `IM_DIRECT_ALL_ORIGIN` — 常量
- `MessageSemantics` / `MessageClassifier` — 语义分类
- `CardUpdateCapability` — 飞书卡片更新

**自建方案：**

```
src/orchestratord/ipc/
├── __init__.py
├── protocol.py          # 消息帧格式、UDS 协议定义
├── client.py            # GatewayIpcClient 替代
├── server.py            # 编排器侧 UDS server（可选）
└── models.py            # InboundMessage / OutboundMessage 替代
```

**协议设计要点：**

- 帧格式：JSONL over UDS，每行一个 JSON 对象
- 帧类型：`DELIVER`（推送消息）、`ACK`（确认）、`PING`/`PONG`（心跳）
- 当前 clawcodex 的 `GatewayIpcClient` 接口（需自建替代）：
  - `__init__(sock: str, instance_id: str)` — 连接 UDS socket
  - `on_deliver: Callable` — 服务端推送消息的回调
  - `send(message) -> None` — 发送消息
  - `stop() -> None` — 断开连接
- 消息模型（纯 dataclass，无外部依赖）：

```python
@dataclass
class InboundMessage:
    origin: str           # 来源标识（如 "feishu:channel_id"）
    text: str             # 原始消息文本
    sender_id: str        # 发送者 ID
    sender_name: str      # 发送者名称
    channel_id: str       # 频道 ID
    channel_type: str     # "feishu" | "slack" | "cli"
    message_id: str       # 平台消息 ID
    semantic: str | None  # 语义标签
    metadata: dict        # 平台特定元数据

@dataclass
class OutboundMessage:
    text: str
    channel: str
    level: str            # "info" | "success" | "warn" | "error"
    markdown: bool
    semantic_tags: list[str]
    metadata: dict
```

**飞书卡片更新：** 不再依赖 `CardUpdateCapability`，改为直接调飞书 Open API 发送/更新卡片消息。`feishu_activity_sink.py` 中已有大部分逻辑，只需替换底层的 API 调用。

**迁移影响：**
- `im_gateway_client.py` — 重写，使用自建 IPC 客户端
- `channel_sink.py` — 替换 `OutboundMessage` 导入
- `feishu_activity_sink.py` — 移除 `CardUpdateCapability` 依赖
- `cli/server.py` — 替换 gateway 相关导入

---

### 2.2 删除：Session 存储/恢复（类别 C）

**决策：** 编排器不再管理 agent session 的持久化和恢复。

**理由：**
- Agent 自身通过 CLI `--resume` 管理会话恢复
- 编排器只需要 `run_id` 用于追踪
- `takeover` 和 `resume` 命令不适合编排器职责

**具体操作：**

1. **删除 `cli/resume_session.py`** — 整个文件，功能由 agent CLI 替代
2. **删除 `cli/takeover.py`** — 整个文件，功能由 agent CLI 替代
3. **清理 `cli/issue.py`** — 移除 `SESSIONS_DIR` 相关引用和 resume 相关命令
4. **清理 `cli/main.py`** — 移除 resume/takeover 子命令注册
5. **清理 `orchestrator.py`** — 移除 session resume 相关逻辑（`_apply_resume_session` 等）

---

### 2.3 删除：工具系统集成（类别 D）

**决策：** 编排器不通过 clawcodex 的 ToolContext 上报进度。

**理由：**
- 编排器不是 agent，不需要"工具"
- 进度上报通过 IM 通道和 status dashboard 完成

**具体操作：**

1. **重写 `progress_sink.py`** — 移除 `ToolContext` 依赖，`ToolContextProgressSink` 改为纯日志/事件输出
2. **删除 `progress_reporter.py`** — 整个文件（back-compat shim，已无调用方）
3. **更新 `agent_runner.py`** — 移除 `ProgressReporter` 的创建和注入

**新进度上报路径：**

```
AgentRunner
  ├── ProgressSink.on_phase_complete()   → 日志 + 事件总线
  ├── ProgressSink.on_turn_complete()    → 日志 + 事件总线
  └── ProgressSink.on_session_complete() → 日志 + 事件总线 + IM 通知
```

---

### 2.4 删除：Provider/模型运行时（类别 E）

**决策：** 编排器不管理 LLM provider，由 agent 后端自行管理。

**具体操作：**

1. **清理 `orchestrator.py`** — 移除 `build_provider_from_config` 导入
2. Issue Clarifier 的 LLM 调用改为通过 agent 单轮分析（见 2.6）

---

### 2.5 内化：Task v2 规范（类别 H）

**决策：** 保留 task 规范注入功能，但将规范文本内化为编排器自身的配置。

**当前实现：** `prompt_builder.py:301` 调用 `task_v2_guidelines()` 从 clawcodex adapter 获取规范文本。该函数映射到 `extensions.orchestrator_runtime.adapters.clawcodex_compat.task_v2_guidelines`，返回的是 Logical Kanban / Task V2 的行为规范文本，指导 agent 使用 `TaskCreate`/`TaskUpdate` 等工具进行任务管理。

**规范文本注入位置：** `PromptBuilder._render_template()` 中，在模板渲染完成后、rules 引用注入后，将规范文本以 `---\n{guidelines}\n---` 格式追加到 prompt 末尾。

**新方案：**

```python
# src/orchestratord/task_guidelines.py
TASK_V2_GUIDELINES = """
## Task Management Guidelines
...（规范文本从 clawcodex 迁移过来，略微调整措辞）...
"""

def get_task_guidelines() -> str:
    return TASK_V2_GUIDELINES
```

**理由：** 规范文本本质上是给 agent 的 prompt 指令，编排器作为"任务分配者"有权定义任务执行规范。文本内容本身不依赖 clawcodex 运行时。

---

### 2.6 改造：Issue 澄清 / Intent Forecast（类别 F、G）

**决策：** 不再调用 clawcodex 的 LLM provider，改为通过 agent 单轮分析。

**当前实现详解：**

**Issue Clarifier 流程**（`issue_clarifier/` 模块，共 6 个文件）：

1. **初始化**（`orchestrator.py:350`）：创建 `IssueClarifierService`，传入 `build_provider_from_config(provider, model)` 作为 provider factory
2. **指纹缓存**（`cache.py`）：对 `{title, description, labels, replies}` 做 SHA-256，命中缓存则跳过 LLM 调用
3. **确定性预检**（`service.py`）：在 LLM 调用前，用正则匹配 `"TBD"`、`"intentionally left unspecified"` 等标记，命中则直接返回 `is_clear=False`（无需 LLM）
4. **LLM 调用**（`service.py:94`）：构造 system + user prompt → `provider.chat(messages, model, max_tokens)` → 解析 JSON 响应
5. **响应解析**（`parser.py`）：fail-open 策略，解析失败则降级为 `is_clear=True`
6. **三通道响应**（`clarification.py`）：
   - Channel 1: StatusDashboard 交互提示（最快，需操作员在线）
   - Channel 2: 文件队列（异步，操作员可离线响应）
   - Channel 3: @mention issue 作者（最慢，超时回退）

**Intent Forecast 流程**（`orchestrator.py:4258`）：

- `compute_workspace_focuses(changed_files, recent_messages)` 将文件路径匹配到预定义的 focus 定义（如 `"orchestrator"`, `"tui"` 等），按权重计算置信度，返回置信度 >= 阈值的 focus 列表
- 仅用于 follow-up 场景（issue 已有分支），将 focus 信息注入 Clarifier 的 LLM prompt 作为上下文

**自建方案：Issue 澄清**

保留现有架构（指纹缓存、确定性预检、三通道响应、解析器），仅替换 LLM 调用层：

```
IssueClarifierService.analyze()
  │
  ├─ 确定性预检（保留，无外部依赖）
  ├─ 指纹缓存（保留，无外部依赖）
  │
  └─ LLM 调用（替换）：
      │
      ├─ 构造分析 prompt（保留 prompt.py 的 prompt 模板）
      │
      ├─ 通过 AgentBackend.create_session() 发起单轮调用
      │   spec = SessionSpec(
      │       cwd=workspace_root,
      │       system_prompt=clarify_system_prompt,
      │       max_turns=1,         # 仅分析，不执行工具
      │       permission_mode="plan",  # 只读分析
      │   )
      │   session = backend.create_session(spec)
      │   response = await session.run(issue_json)
      │
      └─ 解析响应（保留 parser.py）
```

**自建方案：Intent Forecast**

```
_compute_workspace_focus_for_clarifier()
  │
  ├─ 收集 changed files（保留 git diff 逻辑）
  │
  ├─ 构造分析 prompt：
  │   "根据以下文件变更列表，判断涉及的代码领域。"
  │
  └─ 通过 AgentBackend 单轮调用 → 返回 focus areas
```

**注意：** 这两个功能都会产生额外的 LLM 调用成本。如果不需要，可通过 workflow 配置关闭（`clarifier.enabled = false`）。

---

### 2.7 删除：Agent 展开（类别 I）

**决策：** 移除 `@agent-type` 展开功能。

**理由：** 这是 clawcodex 的多 agent 协作功能，编排器的 prompt 构建不需要它。

**具体操作：**

1. **清理 `prompt_builder.py`** — 移除 `_expand_agent_mentions()` 函数及相关导入
2. 如果 issue 描述中包含 `@agent-xxx`，直接保留原文传递给 agent

---

### 2.8 保留：任务注册表（类别 J）

**决策：** 保留 RuntimeTaskRegistry 的使用，但改为通过 SPI 接口调用。

**当前实现：** `agent_runner.py:1110` 在 session 启动时：
1. 导入 `clawcodex_ext.task_registry.RuntimeTaskRegistry` 和 `src.tasks.local_agent.LocalAgentTaskState`
2. 创建 `RuntimeTaskRegistry` 实例
3. 调用 `registry.upsert(LocalAgentTaskState(id=run_id, status="running", ...))`
4. 存入 `session._runtime_tasks`，通过 `SessionSpec.extra` 传递给 backend

**用途：** `_runtime_tasks` 注册表使 `_drain_pending_user_messages` 能在 `ToolResult` 边界触发，实现实时操作员消息注入（`queue_pending_message`）。CLI server 也依赖它来记录待处理消息。

**新方案：** 在 `AgentBackend` SPI 中添加可选方法：

```python
class AgentBackend(Protocol):
    def get_task_registry(self) -> Any | None:
        """Return a runtime task registry, or None if not supported."""
        ...
```

编排器通过 `backend.get_task_registry()` 获取任务注册表，用于管理 agent 运行时任务状态。

---

### 2.9 删除：Dashboard 集成（类别 L）

**决策：** 移除 clawcodex agent dashboard 集成。

**理由：** 编排器有自己的 `status_dashboard.py`，不需要注册到 clawcodex 的 dashboard。

**具体操作：**

1. **清理 `cli/server.py`** — 移除 `OrchestrationSubsystem`、`register_dashboard_source`、`OrchestratorDashboardSource` 导入和调用
2. **清理 `adapters/clawcodex.py`** — 移除对应的 lazy symbol 映射

---

### 2.10 删除：诊断（类别 K）

**决策：** 移除 `FreezeDetector` 依赖。

**理由：** 编排器可以用简单的超时机制替代。

**具体操作：**

1. **清理 `cli/server.py`** — 移除 `FreezeDetector.maybe_start_from_env()` 调用
2. 如需冻结检测，在编排器主循环中加入简单的 wall-clock 超时

---

### 2.11 删除：入口点（类别 M）

**决策：** 移除 `run_orchestrator_subcommand` 依赖。

**理由：** 编排器是独立进程，不再作为 clawcodex 的子命令运行。

**具体操作：**

1. **清理 `im_gateway_client.py`** — 移除 `run_orchestrator_subcommand` 导入和调用

---

### 2.12 迁移：遗留路径（类别 N）

**决策：** 所有 `~/.clawcodex/` 路径迁移到 `~/.orchestratord/`，保留向后兼容读取。

**当前状态：** `workspace_locator.py` 仅定义 `ORCHESTRATORD_BASE`；核心代码不再保留旧的 `CLAWCODEX_BASE` 兼容变量或硬编码路径。

**迁移清单：**

| 文件 | 当前路径 | 新路径 |
|------|---------|--------|
| `workspace_locator.py` | `~/.clawcodex/` (legacy read) | 保持不变（已有兼容逻辑） |
| `report_writer.py` | `~/.clawcodex/tool-events/` | `~/.orchestratord/tool-events/` |
| `modes/pipeline.py` | `.clawcodex/team.json` 等 | `.orchestratord/team.json` 等 |
| `orchestrator.py` | `~/.clawcodex/orchestrator/audit.jsonl` | `~/.orchestratord/audit.jsonl` |
| `orchestrator.py` | `.clawcodex_issue_clarifier_cache.json` | `.orchestratord_issue_clarifier_cache.json` |
| `cli/server.py` | `~/.clawcodex/gateway/gateway.sock` | `~/.orchestratord/gateway.sock` |
| `cli/server.py` | `~/.clawcodex/orchestrator/*/metadata.json` | `~/.orchestratord/orchestrator/*/metadata.json` |
| `cli/issue.py` | `~/.clawcodex/sessions/` | `~/.orchestratord/sessions/` |
| `cli/issue.py` | `~/.clawcodex/orchestrator/audit.jsonl` | `~/.orchestratord/audit.jsonl` |
| `cli/dashboard.py` | `~/.clawcodex/orchestrator/` | `~/.orchestratord/orchestrator/` |
| `session_state.py` | `~/.clawcodex/tool-events/` | `~/.orchestratord/tool-events/` |
| `tool_event_log.py` | `~/.clawcodex/tool-events/` | `~/.orchestratord/tool-events/` |
| `workflow_engine/audit.py` | `~/.clawcodex/workflow-events/` | `~/.orchestratord/workflow-events/` |
| `clarification_queue.py` | `~/.clawcodex/clarification_queue.json` | `~/.orchestratord/clarification_queue.json` |
| `issue_registry/models.py` | 引用 `~/.clawcodex/sessions/` | `~/.orchestratord/sessions/` |

**策略：** 写入始终用新路径；读取时先尝试新路径，必要时由具体后端自行处理旧数据迁移。`workspace_locator.py` 的 `ORCHESTRATORD_BASE` 模式推广到所有文件。

---

## 3. Agent 后端解耦（类别 A）

这是最核心的部分。当前 `agent_runner.py` 深度耦合 clawcodex 的 `QueryRunner`，但 `backend_runner.py` 已经提供了 SPI 路径。

### 3.1 当前状态

```
AgentRunner (agent_runner.py)
  ├── 直接使用 QueryRunner (clawcodex)
  ├── 直接导入 clawcodex 事件类型 (PhaseComplete, TextDelta, ...)
  ├── 直接使用 clawcodex 消息类型 (TextBlock, ToolUseBlock, ...)
  ├── 直接使用 clawcodex builder (build_default_agent_runtime, ...)
  └── 直接使用 ClawcodexBackend (orchestratord_clawcodex)

BackendRunner (backend_runner.py)
  ├── 通过 SPI AgentBackend 接口
  ├── 使用 SPI EventEnvelope (而非 clawcodex 事件类型)
  └── 已经是解耦状态
```

### 3.2 目标状态

```
AgentRunner
  ├── 通过 SPI AgentBackend 接口（与 BackendRunner 统一）
  ├── 使用 SPI EventEnvelope（与 BackendRunner 统一）
  └── 不再直接导入任何 clawcodex 类型

adapters/clawcodex.py
  └── 删除（不再需要 lazy-import shim）
```

### 3.3 策略：统一到 BackendRunner

`BackendRunner` 已经是解耦实现。方案是：

1. **补全 BackendRunner** — 确保它覆盖 AgentRunner 的所有功能（retry、watchdog、control socket、progress sink 等）
2. **删除 AgentRunner** — 将 Orchestrator 切换到 BackendRunner
3. **删除 adapters/clawcodex.py** — 不再需要

**BackendRunner 需要补全的功能：**

- [ ] Retry 逻辑（`_run_with_retry`）
- [ ] Agent watchdog（`AgentWatchdog`）
- [ ] Control socket 集成
- [ ] Progress sink 事件转换
- [ ] Tool event log
- [ ] Transcript 存储
- [ ] Rate limit 处理
- [ ] Noop 检测
- [ ] Goal mode 支持

---

## 4. 实施计划

### Phase 1: 低风险删除（无功能影响）

1. 删除 `cli/resume_session.py`、`cli/takeover.py`
2. 清理 `cli/issue.py` 中的 session resume 引用
3. 清理 `cli/main.py` 中的 resume/takeover 子命令
4. 删除 `progress_reporter.py`（back-compat shim）
5. 移除 `FreezeDetector` 依赖
6. 移除 `run_orchestrator_subcommand` 依赖
7. 移除 `@agent-type` 展开（`prompt_builder.py`）
8. 移除 Dashboard 集成（`cli/server.py`）

### Phase 2: 路径迁移

1. 定义统一的路径管理模块 `src/orchestratord/paths.py`
2. 逐文件迁移 `~/.clawcodex/` → `~/.orchestratord/`
3. 保留向后兼容读取 1-2 个版本

### Phase 3: 功能内化

1. 内化 `task_v2_guidelines` → `task_guidelines.py`
2. 重写 `progress_sink.py` — 移除 ToolContext 依赖
3. 改造 Issue Clarifier — agent 单轮分析
4. 改造 Intent Forecast — agent 单轮分析
5. RuntimeTaskRegistry 通过 SPI 暴露

### Phase 4: IM 网关自建

1. 创建 `src/orchestratord/ipc/` 模块
2. 实现 UDS IPC 协议
3. 重写 `im_gateway_client.py`
4. 更新 `channel_sink.py`、`feishu_activity_sink.py`
5. 更新 `cli/server.py` gateway 相关逻辑

### Phase 5: Agent 后端统一

1. 补全 `BackendRunner` 功能
2. Orchestrator 切换到 `BackendRunner`
3. 删除 `AgentRunner`（或保留为向后兼容别名）
4. 删除 `adapters/clawcodex.py`
5. 删除 `adapters/__init__.py`

### Phase 6: 清理

1. 移除 `pyproject.toml` 中 clawcodex 相关依赖
2. 更新 CI 检查规则（不再需要禁止 `extensions.*` 导入，因为整个依赖已移除）
3. 更新测试

---

## 5. 风险与注意事项

1. **BackendRunner 补全风险：** AgentRunner 经过大量测试，功能复杂。补全 BackendRunner 需要仔细对比和迁移测试用例。

2. **IM 网关兼容性：** 自建 IPC 协议需要与现有 gateway daemon 通信（如果存在），或提供独立的 gateway 实现。

3. **Issue Clarifier 精度：** 用 agent 单轮分析替代专用 LLM 调用，可能影响澄清质量。建议保留配置开关。

4. **路径迁移：** 需要确保向后兼容，现有部署的 `~/.clawcodex/` 数据不丢失。

5. **测试覆盖：** 大量测试文件引用 clawcodex。解耦后需要更新或删除相关测试。

---

## 6. 附录：解耦后的依赖图

```
orchestratord
├── SPI 层
│   ├── AgentBackend (Protocol)     ← 各 backend 实现
│   ├── EventEnvelope               ← 统一事件类型
│   └── SessionSpec                 ← 统一会话参数
│
├── 核心
│   ├── orchestrator.py             ← 零外部依赖
│   ├── backend_runner.py           ← 零外部依赖（仅 SPI）
│   ├── prompt_builder.py           ← 零外部依赖
│   ├── git_sync.py                 ← 零外部依赖
│   └── ...
│
├── IM 通道（自建）
│   ├── ipc/protocol.py             ← UDS 协议
│   ├── ipc/client.py               ← IPC 客户端
│   ├── im_gateway_client.py        ← 消息分发
│   ├── channel_sink.py             ← 事件→IM 消息
│   └── feishu_activity_sink.py     ← 飞书卡片
│
├── 存储（自建）
│   ├── paths.py                    ← 统一路径管理
│   ├── workspace_locator.py        ← 工作区定位
│   └── tool_event_log.py           ← 工具事件日志
│
└── CLI
    ├── main.py                     ← 入口
    ├── server.py                   ← daemon 管理
    ├── issue.py                    ← issue 管理
    └── ...
```
