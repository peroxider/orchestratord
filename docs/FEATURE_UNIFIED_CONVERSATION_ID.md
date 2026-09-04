# 统一会话标识与跨 Agent 会话聚合特性规划

> 状态：草案 v1  
> 范围：orchestratord 编排器、会话持久化与现有 Web Dashboard  
> 覆盖后端：Claude、Codex、Codex App Server、DSH、Hermes、OpenCode、ClawCodex

---

## 1. 背景与问题

当前编排器已经具备后端无关的运行状态和事件协议：

- [`RunSession`](../src/orchestratord/session_state.py) 保存一次运行的编排器状态；
- SPI [`AgentSession`](../src/orchestratord/spi/session.py) 统一 `send()`、`events()`、中断和恢复接口；
- [`EventEnvelope`](../src/orchestratord/spi/events.py) 统一文本、工具调用、工具结果、回合结束和会话结束事件；
- [`_broadcast_to_socket()`](../src/orchestratord/runner_utils.py) 将统一事件转换为 Web/控制 Socket 可消费的帧；
- Dashboard 目前以 `run_id` 读取 `sessions/{run_id}/transcript.jsonl`。

但 `run_id` 代表的是一次 Agent 执行，而不是一条用户可见的完整逻辑会话。Retry、follow-up、Pipeline stage、Debate branch 会产生多个 `run_id`；不同后端也拥有不同格式的原生 `session_id`。

因此当前 Web 页面只能通过 run 或 issue 维度间接拼接历史，无法可靠表达：

```text
一条用户会话
  ├── Claude implementation run
  ├── OpenCode review run
  ├── DSH debate branch A
  └── ClawCodex debate branch B
```

## 2. 目标

### 2.1 功能目标

1. 引入稳定的 `conversation_id`，代表一条逻辑会话。
2. 一个 `conversation_id` 可以包含多个 `run_id`。
3. 每个 run 同时记录编排器身份和 Agent 原生会话身份。
4. transcript 保留统一字段，同时保留后端特有字段和原始事件。
5. Web 根据字段存在性按需渲染文本、工具、审批、推理、用量、阶段和分支信息。
6. 保留现有 `/api/runs/...` 接口和旧 transcript 的读取兼容性。

### 2.2 非目标

- 不要求不同 Agent 共享同一个原生 session；
- 不要求把各后端 transcript 强行转换成完全相同的内部协议；
- 不在本特性中替换现有 JSON registry 或立即引入 PostgreSQL；
- 不把并行 Debate 分支伪装成严格线性的单条消息流。

## 3. 核心身份模型

```text
conversation_id       逻辑会话，生命周期最长，面向用户和 Web
    └── run_id         一次编排器执行，生命周期短，面向日志和控制
          └── backend_session_id
                       Agent 后端原生会话 ID，仅用于后端恢复/调试
```

### 3.1 字段定义

| 字段 | 所属 | 语义 | 生命周期 |
| --- | --- | --- | --- |
| `conversation_id` | 编排器 | 用户看到的一条逻辑会话 | 跨 retry/follow-up/stage/branch 稳定 |
| `run_id` | 编排器 | 一次实际 Agent 执行 | 每次执行唯一 |
| `backend_session_id` | 后端 | Agent CLI/SDK/Server 的原生 session key | 后端决定 |
| `parent_run_id` | 编排器 | 当前 run 的父运行 | 可选 |
| `stage_id` | 工作流 | Pipeline/DAG 阶段标识 | 可选 |
| `branch_id` | 协作模式 | Debate/Swarm 分支标识 | 可选 |

`conversation_id` 不得使用任意后端原生 `session_id`，也不得从 `run_id` 的字符串格式反向推导。

## 4. 会话生命周期规则

| 场景 | `conversation_id` | `run_id` | 关系信息 |
| --- | --- | --- | --- |
| 新 issue | 新建 | 新建 | 无父 run |
| retry | 复用 | 新建 | `parent_run_id=上一 run` |
| follow-up | 复用 | 新建 | `parent_run_id=上一 run` |
| review follow-up | 复用原 issue 会话 | 新建 | `stage_id=review_followup` |
| Pipeline stage | 复用 | 每个 stage 新建 | `stage_id` |
| Debate branch | 复用 | 每个 branch 新建 | `branch_id`、`parent_run_id` |
| 独立手工任务 | 新建 | 新建 | 无父 run |
| 用户主动新建会话 | 新建 | 新建 | 与历史会话断开 |

当前 Pipeline/Debate 中将 `session.run_id` 重置为 `None` 的行为应保留；只允许新增的 `conversation_id` 继续传递。

## 5. 数据模型变更

### 5.1 `IssueRecord`

在 [`issue_registry/models.py`](../src/orchestratord/issue_registry/models.py) 增加：

```python
conversation_id: str | None = None
```

Issue registry 是 issue-to-PR 主链路中最合适的稳定存储位置。首次创建运行前，如果字段为空则生成 UUID 并立即持久化；后续 retry、follow-up、review 使用同一值。

建议将 registry schema version 提升到 2。旧记录缺少该字段时允许按默认值加载，并在下一次进入逻辑会话时补齐。

### 5.2 `AgentTask`

在 [`agent/task.py`](../src/orchestratord/agent/task.py) 增加显式字段：

```python
conversation_id: str | None = None
```

不应只通过 `context` 传递，因为它是编排层核心身份，而非 workflow-specific opaque data。

### 5.3 `RunSession`

在 [`session_state.py`](../src/orchestratord/session_state.py) 增加：

```python
conversation_id: str | None = None
parent_run_id: str | None = None
backend_name: str | None = None
backend_session_id: str | None = None
stage_id: str | None = None
stage_name: str | None = None
branch_id: str | None = None
```

其中 `backend_session_id` 在 `backend.create_session(spec)` 返回 SPI session 后回填。

### 5.4 `SessionSpec`

`SessionSpec.resume_session_id` 继续只表示“要恢复的后端原生会话”。不得将它改造成 `conversation_id`。

可以增加非后端语义的元数据字段，或通过现有 `extra` 传递：

```python
extra = {
    "conversation_id": "...",
    "parent_run_id": "...",
    "stage_id": "...",
    "branch_id": "...",
}
```

这些字段只用于日志、transcript 和观测，不应被后端当作原生 resume key。

### 5.5 通用 workflow

对于声明式 workflow，在 [`WorkflowState`](../src/orchestratord/workflow_engine/workflow_state.py) 增加：

```python
conversation_id: str | None = None
```

`StageRunner` 创建 `AgentTask` 时传入该值；`WorkflowResult` 和 `StageResult` 也应返回该值，便于 Web 聚合和 checkpoint 恢复。

### 5.6 手工运行

[`RunRecord`](../src/orchestratord/run_store.py) 增加：

```python
conversation_id: str | None = None
```

旧数据可将 `conversation_id` 默认设为 `run_id`，保证已有手工运行仍能被查询。

## 6. 会话创建与传播

建议新增一个编排器侧 helper，集中处理身份创建：

```python
def ensure_conversation_id(existing: str | None = None) -> str:
    return existing or str(uuid.uuid4())
```

Issue 主流程建议顺序：

1. 从 `IssueRecord.conversation_id` 读取；
2. 为空时生成 UUID；
3. 立即保存 registry；
4. 创建 `RunSession(conversation_id=...)`；
5. 每次实际执行生成新的 `run_id`；
6. Agent session 创建后记录 `backend_session_id`；
7. 所有事件和 transcript 写入统一身份元数据。

不得通过解析类似 `timestamp_issue_id` 的 `run_id` 推导会话关系。当前 Dashboard 的历史合并逻辑应改为读取 conversation manifest。

## 7. Transcript v2

### 7.1 设计原则

- 保留现有 `role`、`content`、`ts` 字段，兼容旧 CLI/Web；
- 增加统一 envelope 元数据；
- 统一字段用于通用渲染；
- `raw` 用于后端特有字段和未来协议字段；
- 未知事件不得静默丢弃。

### 7.2 推荐记录格式

```json
{
  "schema_version": 2,
  "conversation_id": "conv-123",
  "run_id": "run-456",
  "backend": "claude",
  "backend_session_id": "native-789",
  "stage_id": "implementation",
  "branch_id": null,
  "parent_run_id": null,

  "seq": 42,
  "timestamp": 1756950000.123,
  "kind": "tool_call",
  "role": "assistant",
  "content": [],

  "text": null,
  "tool": {
    "call_id": "call-1",
    "name": "Read",
    "arguments": {},
    "result": null,
    "output": null,
    "ok": null,
    "is_error": null
  },
  "approval": {
    "request_id": null,
    "decision": null,
    "message": null,
    "deny_reason": null
  },
  "reasoning": {
    "text": null,
    "is_summary": false
  },
  "usage": {
    "input_tokens": null,
    "output_tokens": null,
    "total_tokens": null,
    "cost_usd": null
  },
  "lifecycle": {
    "turn": null,
    "turn_delta": null,
    "phase": null,
    "status": null,
    "reason": null,
    "error_code": null,
    "error_message": null
  },
  "raw": {}
}
```

### 7.3 当前后端字段覆盖

统一层至少应覆盖以下现有字段：

| 类别 | 字段 |
| --- | --- |
| 身份 | `session_id`、`sessionId`、`run_id`、provider、model |
| 消息 | `text`、`delta`、`content`、`role` |
| 工具 | `call_id`、`tool_use_id`、`toolCallId`、`callId`、`name`、`tool_name`、`arguments`、`input`、`params` |
| 工具结果 | `result`、`output`、`ok`、`is_error`、`isError` |
| 审批 | `request_id`、`call_id`、`tool_name`、`arguments`、`message`、`decision`、`deny_reason` |
| 推理 | `thinking`、`reasoning-delta`、推理文本或摘要 |
| 生命周期 | `turn`、`turn_delta`、`phase`、`turn_count`、`reason`、`status` |
| 错误 | `code`、`message`、`error_code`、`error_message` |
| 用量 | `usage`、input/output/total tokens、`total_cost_usd` |

后端适配器无法理解的字段放入 `raw`，不要丢弃。

## 8. Conversation Manifest

保留现有按 run 存储的 transcript：

```text
~/.orchestratord/sessions/{run_id}/transcript.jsonl
```

新增会话索引：

```text
~/.orchestratord/conversations/{conversation_id}/conversation.json
```

示例：

```json
{
  "schema_version": 1,
  "conversation_id": "conv-123",
  "issue_id": "issue-1",
  "created_at": 1756950000,
  "updated_at": 1756950300,
  "status": "running",
  "runs": [
    {
      "run_id": "run-1",
      "backend": "claude",
      "backend_session_id": "claude-1",
      "stage_id": "implementation",
      "branch_id": null,
      "parent_run_id": null,
      "started_at": 1756950000,
      "finished_at": null
    }
  ]
}
```

不建议第一阶段把并行 run 物理合并为一个文件。Manifest + 每 run transcript 可以避免 Debate/Swarm 并发写入冲突，也可以保留阶段和分支结构。

## 9. Web API 与前端

### 9.1 API

现有接口继续保留：

```text
/api/runs
/api/runs/{run_id}/events
/api/runs/{run_id}/messages
```

返回数据增加：

```json
{
  "run_id": "run-1",
  "conversation_id": "conv-123",
  "backend": "claude",
  "stage_id": "implementation",
  "branch_id": null
}
```

新增聚合接口：

```text
GET  /api/conversations
GET  /api/conversations/{conversation_id}
GET  /api/conversations/{conversation_id}/events
POST /api/conversations/{conversation_id}/messages
```

`/events` 应根据 manifest 读取多个 run 的 transcript，并为每条事件保留 `run_id`、stage 和 branch 元数据。

### 9.2 渲染规则

前端不假设所有字段必然存在：

| 存在字段 | 渲染内容 |
| --- | --- |
| `text` 或文本 content | 消息气泡 |
| `tool` | 工具调用/结果卡片 |
| `approval` | 审批卡片 |
| `reasoning` | 可折叠推理区 |
| `usage` | token/cost 信息 |
| `lifecycle.phase` | 阶段节点 |
| `lifecycle.error_message` | 错误面板 |
| `stage_id` | 阶段标签 |
| `branch_id` | 分支标签 |
| 未知 `kind` 或非空 `raw` | 原始事件折叠区 |

字段不存在时不显示空占位；未知事件必须可见但不应阻塞其他消息渲染。

并行 Debate/Swarm 不应简单按时间压扁成一条线，建议至少显示 branch 标签；后续可以增加泳道或阶段视图。

## 10. 兼容与迁移

1. 旧 `IssueRecord` 没有 `conversation_id` 时允许加载。
2. 旧 transcript 继续按现有 `role/content/ts` 读取。
3. 旧 run 没有会话 manifest 时，首次访问可按 `run_id` 创建兼容 manifest，或暂时将 `conversation_id` 设为 `run_id`。
4. 现有 `/api/runs/...` 行为不变。
5. 新 Web 优先使用 `conversation_id`；传入 `run_id` 时继续展示单 run 视图。
6. 不修改 Agent 原生 resume 逻辑；`resume_session_id` 只接受后端原生 ID。

## 11. 并发与一致性

第一阶段假设一个 Dashboard/daemon 主进程负责写入同一会话 manifest：

- manifest 使用临时文件 + 原子替换；
- transcript 继续 append-only；
- 每个事件携带 `conversation_id` 和 `run_id`；
- 不要求并行 branch 共享全局严格序号。

如果未来支持多 daemon、多主机或高频并发写入，应将 manifest 和事件索引迁移到 SQLite/PostgreSQL，并为事件增加全局 `event_id` 和可查询索引。

## 12. 实施阶段

### Phase 1：身份贯通

- [ ] `IssueRecord` 增加 `conversation_id`；
- [ ] `AgentTask`、`RunSession` 增加会话字段；
- [ ] 新建 issue、retry、follow-up、review follow-up 复用会话 ID；
- [ ] Pipeline/Debate 只重置 `run_id`，继续传递 `conversation_id`；
- [ ] BackendRunner 记录 `backend_session_id`。

### Phase 2：持久化与事件增强

- [ ] 实现 Conversation Manifest；
- [ ] transcript 增加 schema v2 元数据；
- [ ] `_broadcast_to_socket()` 保留 envelope 元数据；
- [ ] 各后端补充 `raw` 和特有字段；
- [ ] 未知事件不再静默丢弃；
- [ ] 初始用户 prompt 以统一 UserMessage 写入 transcript。

### Phase 3：Web 聚合

- [ ] `/api/runs` 返回 `conversation_id`；
- [ ] 增加 conversation 查询和 SSE；
- [ ] 替换基于 run_id 命名规则的历史合并；
- [ ] 前端按字段存在性渲染；
- [ ] 增加阶段、分支、后端标签。

### Phase 4：验证与增强

- [ ] 六类后端的事件字段契约测试；
- [ ] retry/follow-up/pipeline/debate 聚合测试；
- [ ] 旧 transcript/registry 兼容测试；
- [ ] 并发 manifest 写入测试；
- [ ] 未知事件和 `raw` 展示测试。

## 13. 验收标准

### 13.1 身份

- 同一 issue 的 retry/follow-up 前后 `conversation_id` 不变；
- 每次实际执行的 `run_id` 唯一；
- Agent 原生 `session_id` 不被误当作 `conversation_id`；
- Pipeline/Debate 的所有 stage/branch 都能关联到同一个顶层会话。

### 13.2 数据

- transcript 同时包含统一字段和后端原始扩展字段；
- Claude 的 thinking/system、DSH 的 reasoning/usage、OpenCode 的 approval 等可追溯；
- 未知事件不会导致事件丢失或 Web 页面失败；
- 旧 transcript 仍可通过 CLI 和 Web 读取。

### 13.3 Web

- Web 可以按 conversation 查看多个 run 的完整记录；
- 文本、工具、审批、推理、阶段、分支、用量字段按需显示；
- 缺少某字段的 Agent 不显示空白组件；
- 并行分支不会互相覆盖或破坏 transcript。

## 14. 设计结论

本特性不改变现有后端原生会话机制，而是在编排器之上新增一层稳定的逻辑会话身份：

```text
Conversation identity  →  conversation_id
Execution identity     →  run_id
Backend identity       →  backend_session_id
```

这样既能保持现有 Agent SPI 和按 run 存储的优势，又能让 Web 以一致方式聚合跨 Agent、跨阶段、跨重试的完整会话历史。

## 15. 测试覆盖审查结果

新增契约测试位于
[`tests/test_unified_conversation_contract.py`](../tests/test_unified_conversation_contract.py)，覆盖：

- `conversation_id` 在 `IssueRecord`、`AgentTask`、`AgentTaskResult`、`RunSession`、`RunRecord` 和 `WorkflowState` 之间的传播；
- `SessionSpec.extra` 与后端原生 `resume_session_id` 的语义隔离；
- `backend_session_id`、父子 run、stage 和 branch 的映射；
- transcript v2 的身份/envelope/raw 元数据；
- Conversation Manifest 的跨 run 索引和 Dashboard 会话聚合入口；
- OpenCode approval、Claude thinking/system 以及错误/审批字段的归一化契约；
- 最大公共字段集和现有 Web 投影兼容性。

当前实现尚未包含上述规划能力，因此未实现部分使用 `pytest.mark.xfail(strict=True)` 固化：实现后若仍失败会暴露缺口，若意外提前实现也会因 XPASS 失败，避免测试假绿。当前新增文件的基线为 `2 passed, 16 xfailed`；相关既有核心/后端回归为 `99 passed, 16 xfailed`。

后续实现完成的验收条件是：逐步消除这些严格 `xfail`，并补充 retry、follow-up、Pipeline、Debate 并发、旧 transcript/registry 兼容和未知事件 `raw` 展示的集成测试。
