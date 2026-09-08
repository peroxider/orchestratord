# orchestratord vs multica — 特性缺口开发文档

> 状态：v2（实测同步 + 单用户模式决策）
> 作者：orchestratord 团队
> 取代：v1（2026-09-04 草案，§14 变更记录）
> 配套：`docs/FEATURE_GAP_VS_MULTICA_DETAILED.md`（实测文件/行数/路由/能力对照）
> 范围：在保留 orchestratord 现有架构优势（能力矩阵 / SPI / agent-callable Skills / in-process 多 agent 模式）的前提下，**以单用户模式**补齐 multica 重要的可视化与实时特性。客户端形态仅 Web；Desktop 与 Mobile 暂不实现（D5）。

---

## 0. 元信息

### 0.1 文档目的

v1 是"空地规划"——草案阶段 describe 一个完整产品形态。v2 同步了**当前代码实际状态**：Phase 0–2 的 FastAPI / PostgreSQL / WebSocket / Next.js 骨架、15 个 backend 包、`packages/{core,ui,views}` 视图层已落地，但实现深度仅是路由级 stub，Phase 3–5（chat / Slack-Lark OAuth / 调度器 / 拖拽看板 / 多 agent 模式可视化 / 真 ACP 适配）几乎未动工。v2 重新划定单用户模式下的优先级，把"Realtime + session control core"作为 Phase A，把其他维度排到后续 Phase。

### 0.2 与既有文档的关系

- **本文件** = 单用户模式下的产品规划；含范围、保留约束、阶段路线、验收
- `docs/FEATURE_GAP_VS_MULTICA_DETAILED.md` = v1 同期实测对比（文件 / 行数 / 路由 / backend 协议族覆盖率）
- `docs/FEATURE_UNIFIED_CONVERSATION_ID.md` = 跨后端 conversation 同一性方案
- 涉及的 backend 覆盖（缺口）会与 `DESIGN_backends_hardening.md` 保持锁步

### 0.3 v2 同期决策

| ID | 决策 | 理由 |
| --- | --- | --- |
| **D5** | 仅 Web 客户端；Desktop / Mobile 不做 | electron-builder 与 Expo + App Store 发布链路另立专项 |
| **D6** | PostgreSQL 17（含 `pgcrypto` / `pg_trgm`） | 与 multica 同主版本，扩展生态一致 |
| **D7** | Web 后端框架 = FastAPI | 与 Python 一致；原生 OpenAPI + WebSocket + SSE |
| **D8** | Web 前端框架 = Next.js 16 App Router + TanStack Query + Zustand | 与 multica 共享心智模型 |
| **D9** | **单用户模式**为当前阶段基线 | 多租户 / 多用户场景暂不补（见 §4） |
| **D10** | 多租户**数据层**保留，**UI 不暴露** | 后续切换多用户时不需要数据迁移 |
| **D11** | Phase A = Realtime + session control core | 用户在 2026-09-07 选定 |

### 0.4 v2 优先级（单用户模式）

| 优先级 | 维度 | 来源（multica） | 章节 |
| --- | --- | --- | --- |
| **Phase A** | 真 WebSocket pub/sub + 会话控制（approve / deny / pause / resume / stop）+ 拖拽看板 + 多 agent 模式可视化 | §5.4 / §5.2.3 / §5.2.1 | §5 |
| Phase B | 通讯层：workspace-level chat + mention 路由 + Slack/Lark 真 OAuth + 通知推送 | §7.4 / §7.5 | §6 |
| Phase C | 调度与可观测：autopilot scheduler + token cost 估算 + 用量聚合 + inbox 详情 | §7.3 / §5.2.4 / §5.2.6 | §7 |
| Phase D | 后端协议覆盖：落地 `orchestratord-acp` 通用适配 + `protocol_family` / `runtime_id` 分离 + 5 个 stub backend 补齐 | §8 | §8 |
| Phase E | 文档站深耕 + i18n 三语 + 测试烟囱 | §5.6 | §9 |

### 0.5 Phase B–E 实施状态与剩余待完成项（2026-09-08 审计）

对照代码全面审计（3 路探查 + 实现方案设计）后的结论：Phase A 全部落地；Phase B–E **绝大部分已实现**（多数早于 Phase A 会话经 67c5c2c / d1b9b7c / 25dc9a0 / dd55d06 / 1f2a929 / c85485e / 3b7428e 等提交进入）。审计发现的 4 个功能缺口已于同日（2026-09-08）全部闭环，§6.6 / §7.5 验收成立。

**已落地（无需重做）**：

| 维度 | 已落地证据 |
| --- | --- |
| B.1 chat 主链路 | `chat_dispatcher.py` / `chat_daemon.py` / `chat_bridge.py`（dispatcher → daemon → 真实 BackendRunner → 折叠落库）；`POST /api/workspaces/{ws}/chat/sessions` + messages 端点；`Message` 表（db/models/sessions.py:69）；前端 chat 页（暂 2s 轮询） |
| B.3 / B.4 / B.5 | `integrations/oauth.py`（SlackOAuth / LarkOAuth）+ `/slack/authorize` `/slack/callback` `/integrations/slack/events` 路由；`vcs.py` + `github_app.py` |
| C.1 调度器 | `scheduler/autopilot.py`（croniter，start/stop/tick/claim_slot/mark_failed），`serve` lifespan 已启动（api/app.py:78-89） |
| C.2 成本估算器 | `cost/estimator.py`（`load_pricing` + `estimate_cost_usd`）+ `tests/cost/test_estimator.py`；usage 页已渲染 USD |
| C.3 / C.4 | usage-charts / usage-page 组件 + usage 路由；inbox 已拆 approval / clarification / failure 三卡片 |
| D 全部 | ACP 通用包（一个包导出 codebuddy / qodercli / qoderclicn / deveco descriptor）；`protocol_family` / `runtime_id` 两层派生；5 个 ex-stub 真翻译（copilot 623 / cursor 815 / kimi 1084 / reasonix 1247 / zeroclaw 961 LOC）+ kiro-cli 299 LOC + opencode 重写 1094 LOC |
| E 全部 | apps/docs Fumadocs；locales en / ja / zh-CN；`agentintegration` pytest 标记（pyproject 默认排除真实 CLI） |

**审计缺口闭环记录（2026-09-08，全部已落地）**：

| # | 缺口 | 实施结果 | 关键落点 | 详见 |
| --- | --- | --- | --- | --- |
| 1 | **usage 聚合写入** ✅ | 完成的 run 自动落一行聚合：`UsageAggregateRepository.upsert`（PG `ON CONFLICT` 幂等累加，`uq_usage_aggregates_bucket` NULLS NOT DISTINCT）；`POST /usage` router 复用同一 upsert；`chat_daemon.py` 给 `AgentTask` 塞 `context={workspace_id, agent_id, issue_id}`；`backend_runner.py::_record_usage()` 在 `run_task` 完成后 best-effort 写入（cost==0 时用 `_snapshot_model` 估算，失败绝不影响 run） | `db/repository.py` / `api/routers/usage.py` / `chat_daemon.py` / `backend_runner.py`；测试 `tests/db_integration/test_repository_queries.py`、`tests/api/test_usage_api.py`、`tests/api/test_chat_daemon.py::TestUsageAggregation` | §7.2 / §7.3 |
| 2 | **mention 文本解析** ✅ | 显式 id 优先，都为空时 `parse_mentions` 逐 handle 查 `agents.by_name` / `members.by_name`（agent 优先、按出现顺序取首个命中），解析不出维持 422；session 绑定解析出的目标 | `db/repository.py::MemberRepository.by_name`；`api/routers/issues.py` mention 处理器；测试 `tests/api/test_issues_api.py::TestMention` | §6.2 |
| 3 | **chat 流式推送** ✅ | bridge sink 发布 `chat.{session_id}` 帧（text / text_delta / tool_call / tool_result；`turn_complete` / `session_complete` / `error` 终结帧在 DB commit 之后发布）；前端 `RealtimeClient` 加 `unsubscribe` + `addMessageListener`，`useRealtimeBridge` 暴露 `getActiveRealtimeClient`，新增 `useRealtimeSubscription` hook，`invalidationFor` 映射 `chat.` → `['chat-messages', id]`，chat 页流式缓冲气泡（无 socket 时回落 2s 轮询） | `chat_bridge.py`、`packages/core/src/realtime/*`、`packages/views/src/chat/{chat-page.tsx,use-chat-stream.ts}`；测试 `tests/api/test_chat_bridge.py::TestRealtimePublishing`、`client.test.ts`、`messages.test.ts` | §6.1 |
| 4 | **daemon 调度器** ✅ | `orchestratord server start` 独立 daemon 也拉起 `AutopilotScheduler`（`subsystem.run()` 前 start、finally 里 stop），与 serve 路径同受 `ORCHESTRATORD_AUTOPILOT_DAEMON=1` 门控 | `cli/server.py::_run()` | §7.1 |

本文档 §6–§9 各节的"状态"注记与 §8 的 LOC 表修正已随本次审计同步更新。

---

## 1. 范围与非目标

### 1.1 In scope（本期 v2 周期）

| 项 | 内容 | 章节 |
| --- | --- | --- |
| 单用户模式开关 | Web 端默认进入唯一 workspace，URL `slug` 与 `workspace_id` 一致 | §4.1 |
| 真 WebSocket pub/sub | topic-based 转发 daemon → server → browser，in-process `asyncio.Queue` 单实例 | §5.1 |
| 会话控制（端到端） | `POST /api/sessions/{id}/{approve,deny,pause,resume,stop}` 接 `BackendRunner` + capability 校验 | §5.2 |
| 拖拽看板 | `@dnd-kit` 在 kanban 列间拖动触发 `PATCH /issues/{id} { status }` 乐观更新 | §5.3 |
| 多 agent 模式可视化 | pipeline / debate / swarm / coordinator / single 各自专属渲染 | §5.4 |
| 工具调用卡片 | `TOOL_CALL` + `TOOL_RESULT` 染色卡片、4KB 截断、`transcript-dialog` 浮层 | §5.5 |
| Web provider tree 补全 | `QueryClientProvider` + `I18nProvider` + `ThemeProvider` + `AuthGate` | §5.6 |
| **Runtime 机器接入** | daemon PATH 探测 + 自定义运行时配置 + task env 注入（5 个集成契约）+ `workspaces_root` + 私有/公开 visibility + 并发上限 | §4.4 |
| 多租户数据层保留 | `workspaces / members / squads / projects / tokens / audit` 表与 router 保留，**路由层不暴露** | §4.2 |
| 测试烟囱 | 真 agent CLI 烟雾测试走 `agentintegration` build tag；CI drift detector 锁住 `BackendCapabilities` | §11 |

### 1.2 Out of scope（v2 明确不做）

| 项 | 不做的理由 | 章节 |
| --- | --- | --- |
| **多租户 / 多用户 UI** | D9；多租户数据层已建好但本期 UI 不暴露 | §4 |
| Desktop 客户端 | D5 | — |
| Mobile 客户端 | D5 | — |
| SaaS / 商业化 / billing | 仅做自托管；`entitlement` 表预留 | §4.3 |
| 全部 20 个剩余 CLI | 只补 5 个 stub backend 真翻译 + 落地 ACP 通用包 | §8 |
| 自定义 runtime profile 协议白名单 | 复用 `SupportedTypes` 内置集合 | §8.4 |
| 多 VCS 后端 | GitHub only；GitLab / Gitea / Forgejo 后置 | §7.5 |
| 通知渠道 ≥ 3 | Slack / Lark 起步；DingTalk / WeCom / Telegram 后置 | §6.5 |
**multica 独占 / orchestratord v2 明确放弃的可视化能力**（Phase E 之后逐项评估）：

| 能力 | multica 实现位置 |
| --- | --- |
| Gantt 视图 | `packages/views/issues/components/gantt-view.tsx` |
| 批量操作工具栏 | `packages/views/issues/components/batch-action-toolbar.tsx` + `.confirm.test.tsx` |
| Task transcript 浮层对话框（含 diff-highlight） | `packages/views/common/task-transcript/{agent-transcript-dialog,build-timeline,run-timeline,diff-highlight}.{ts,tsx}` |
| 多态 assignee picker（member / agent 单选组件） | `packages/views/issues/components/board-card-assignee-picker.tsx` |
| Comment-trigger-chips（评论触发器） | `packages/views/issues/components/comment-trigger-chips.tsx` |
| Tiptap 富文本编辑器 + 扩展 | `packages/views/editor/{extensions,hooks,styles,utils}/` |
| Onboarding 多步编排（templates / steps / components） | `packages/views/onboarding/{steps,templates,components}/` |
| Command palette + Floating chat | `packages/views/search/` + `packages/views/chat/floating-chat.tsx` |
| Inbox 三类子视图（APPROVAL_REQUEST / failure / clarification） | `packages/views/inbox/components/` |
| Runtime daily/weekly charts | `packages/views/runtimes/components/charts/{daily,weekly}-tasks-chart.tsx` |
| Audit 页（web 专属，与 sessions/agents 等共享域不同） | `apps/web/app/[workspaceSlug]/(dashboard)/audit/` |
| 富文本 attachments / labels / invitations / invite 域 | `packages/views/{attachments,labels,invitations,invite}/` |

v2 周期内不补这些 —— 单用户模式（D9）+ Realtime/session-control（Phase A）+ 后端协议覆盖（Phase D）已是 v2 全部投入；multica 形态上的体验特性留到 Phase E 之后或社区自建。

| `apps/desktop` / `apps/mobile` | D5 | — |

---

## 2. 缺口总览（v2 实测基线）

> 与 v1 §2 的差异：用"已落地"vs"未落地"重新打分。

| 维度 | 当前 orchestratord（v2 实测） | multica 形态 | 缺口 | 章节 |
| --- | --- | --- | --- | --- |
| Web 客户端 | `apps/web` 14 个 stub 页面（每页 ≤ 61 行） + `packages/views` 35 个文件 | `apps/web` 28 个 .tsx/.tsx（含 `issues/[id]`） + `packages/views` 1050 个文件 | 严重 | §5 |
| **多客户端形态** | 仅 Web | Web + Desktop + Mobile | 严重（仅 Web 优先） | D5 |
| **多租户** | 数据层已建（24 张表含 `workspaces`/`members`/`squads`/`projects`/`tokens`/`audit`），API router 18 个，UI **不暴露**（D10） | workspaces + 角色 + access scopes | 单用户模式（D9）下不补 | §4 |
| 持久化 | PostgreSQL 17（已就位）+ 42 个 alembic migrations | PostgreSQL 17 + 120+ migrations + sqlc | **持平** | §4.3 |
| **Runtime（机器/守护进程）** | 仅 Runtime 实体 + WS heartbeat 30s 骨架（`domain/runtime.py` 87 行 + `runtime/live_registry.py` 86 行 + `api/routers/runtimes.py` 149 行）；**无 daemon 端 PATH 扫描、自定义运行时配置、task env 注入、`workspaces_root`、私有/公开 visibility、并发上限** | daemon 启动 PATH 扫描 26 个 CLI + 自定义运行时配置（协议族 + 固定参数 + 命令转义 + 协议 flag 剔除 + 模型覆盖）+ 5 个集成契约 env var（`MULTICA_TOKEN` / `MULTICA_TASK_ID` / `MULTICA_AGENT_ID` / `MULTICA_WORKSPACE_ID` / `MULTICA_SERVER_URL`）+ `workspaces_root` 三级覆盖（flag > `MULTICA_WORKSPACES_ROOT` env > profile）+ 私有/公开 runtime + 15s 心跳 / 3min 离线宽限 / 7 天自动清理 + daemon 全局并发默认 20 / 单 agent 默认 6 | **严重** | §4.4 |
| **实时** | WS 路由 stub（`/ws` heartbeat + subscribe/unsubscribe）；SSE 旧 LiveView 1464 行仍可用 | WebSocket + topic-based pub/sub + 30s 心跳 | **严重** —— pub/sub backbone 未落地 | §5.1 |
| **会话控制** | router stub（ack 响应，未接到 BackendRunner） | approve / deny / pause / resume / stop 全链路 | **严重** | §5.2 |
| 后端覆盖 | 15 包（含 `orchestratord-acp`）；其中 5 个 stub（copilot/cursor/kimi/openclaw/reasonix/zeroclaw，296~302 行）；`kiro-cli` 仅 pyproject | 26 protocol family + 1 builtin runtime (`omp` → `pi`)；每个 family 平均深度 ~600 行 + 完整 test | 中等 | §8 |
| 协作抽象 | 5 in-process modes（single/coordinator/pipeline/debate/swarm） | Squads / Projects / Autopilots | 单用户模式（D9）下 squads/projects 不补 UI；autopilots 保留 | §4 |
| **多 VCS** | Linear + GitHub（router stub） | GitHub / GitLab / Gitea / Forgejo | GitHub only；其余后置 | §7.5 |
| 通知渠道 | Slack / Lark adapter 已写（`notifications/adapters.py`），无 OAuth handshake | Slack / Lark / DingTalk / WeCom / Telegram 五件 | 中等 | §6.5 |
| **chat** | ❌ 无 chat 域 | multica 完整 chat 模块（含 floating chat + mention 触发） | **严重** | §6.1 |
| **mention 路由** | `parse_mentions()` 已写；`/mention` route 仅 ack | mention → 触发 agent session | **严重** | §6.2 |
| **autopilot scheduler** | CRUD 已写；无调度循环 | APScheduler 周期触发 → 启动 workflow | **严重** | §7.1 |
| **拖拽看板** | kanban toggle 在 UI，但列间拖动未实装 | `@dnd-kit` 拖动 + 乐观更新 | 中等 | §5.3 |
| **多 agent 模式可视化** | 仅扁平 `event-timeline` | pipeline / debate / swarm / coordinator 各自拓扑 | 中等 | §5.4 |
| **工具调用卡片** | `event-timeline` 通用渲染；无 4KB 截断、无 diff 着色、无 transcript 浮层 | transcript dialog + build-timeline + diff-highlight | 中等 | §5.5 |
| Web provider tree | `CoreProvider` + `I18nProvider` 已挂；缺 `QueryClientProvider` / `ThemeProvider` / `AuthGate` | `web-providers.tsx` 三件套完整 | 中等 | §5.6 |
| 计费 / 配额 | 无 | 完整 SaaS + entitlement | 不做（D5） | — |
| 文档站点 | Fumadocs 骨架（`apps/docs`） | Fumadocs + i18n | 半缺（Phase E） | §9 |

详细差距见 `docs/FEATURE_GAP_VS_MULTICA_DETAILED.md` §1、§2。

---

## 3. 必须保留的 orchestratord 优势特性

> 约束。任何"补 multica 的形态"不得削弱下列属性。

### 3.1 SPI 与插件机制

保留对象：

- `src/orchestratord/spi/` 下的 `AgentBackend` / `AgentSession` / `BackendCapabilities` / `EventEnvelope` / `ApprovalPolicy`
- `importlib.metadata` 解析 `orchestratord.backends` + `orchestratord.backend_descriptors` 双层 entry point
- CI 强制 core 不直接 import 任何 backend 包（`tests/test_capability_drift.py` 守门）

Web 层不能绕过 SPI：所有 agent 交互必须经过 `BackendRunner` → `AgentSession.events()` 流；**不允许** Web 直接 fork agent 进程或读 backend 私有协议。

### 3.2 能力矩阵 + 中央强制降级

保留对象：

- `spi/capabilities.py` 的 8 位能力（`streaming_deltas` / `resumable` / `interrupt` / `approval_hooks` / `parallel_sessions` / `cost_reporting` / `tool_filtering` / `takeover`）
- `spi/degradation.py` 的 8 条降级路径
- "backend 不得自降"原则

Web 前端必须忠实呈现能力位：

- UI 上能展示"此 backend 当前不支持 streaming，是否降级为整段渲染"，而不是假设所有 backend 都流式输出
- `CapabilityMatrix` 组件（已在 `packages/views/src/agents/`）继续作为权威渲染入口，**不**改为 multica 的可选项 token 字典

### 3.3 agent-callable Skills + 可验证引用

保留对象：

- `src/orchestratord/skills/builtin/*/SKILL.md` 的 YAML frontmatter（`name` / `description` 必需）
- `skills/builtin/*/references/source-map.md` 的 SHA256 锚点
- `skills verify` CLI + `scripts/regen_source_map.py`
- `BackendRunner` 在 session start 注入一行 / skill 索引到 system prompt

Web 前端的 Skills 页面**必须**显示每条 skill 的 source-map 引用状态（`verified` / `stale`），并提供"refresh hashes"按钮（在线调用 `scripts/regen_source_map.py` 或对应 Python 函数）。

### 3.4 in-process 多 agent 模式

保留对象：

- `src/orchestratord/modes/` 下 5 个 `ModeRunner`（`single` / `coordinator` / `pipeline` / `debate` / `swarm`）
- `ModeDecision` dataclass + `ModeSelector` 路由
- `swarm_checkpoint.json` 检查点恢复机制
- debate 模式的独立性约束（proposer 不得读对方输出）

Web 前端的 Sessions 页面把"模式"作为 first-class 维度展示（见 §5.4），并提供 pipeline / debate / swarm / coordinator 各自的可视化。

### 3.5 一键 install.sh + 后端探测

保留对象：

- `install.sh` 的 backend 探测表（当前 15 个 entry）+ `auto-detect` 逻辑
- `--backends X,Y` / `--no-backends` / `--all-backends` / `--dry-run` 模式
- `~/.orchestratord/venv` + `activate.sh` 隔离

Web 部署时 install.sh 需要扩展到：

- 新增 `--with-web` flag（启动 FastAPI + 引导 Next.js 静态资源）
- 新增 `--db` 子命令（init / migrate / reset）
- 但**不破坏**现有 CLI 兼容

### 3.6 嵌入式 LiveView 简版

保留对象：

- `src/orchestratord/cli/dashboard.py` 作为"零依赖开发者模式"继续可用
- 内置事件 feed、SSE、chat UI

Web 上线后这个 LiveView 仍要保留 1 个 release cycle（标记 deprecated），避免强制迁移破坏开发者机器。后续随 Web 稳定逐步下线。

---

## 4. 单用户模式决策（D9 / D10）

> v2 关键决策。

### 4.1 单用户模式语义

- Web 端**不暴露** `/workspaces/{slug}/...` URL 切换器；登录后默认进入 `default` workspace，URL 形如 `/default/...`
- Web 端**不暴露** members / tokens / audit / squads / projects / invitations 路由
- 多租户 API router（`members` / `tokens` / `audit` / `squads` / `projects`）**保留在 FastAPI app 内**，可被 CLI 或脚本调用，但**不进入** Web 侧菜单
- 一个 install 默认创建一个 `default` workspace，单一 owner 角色（内部 marker，无 UI 暴露）

### 4.2 多租户数据层保留清单

| 表 / 路由 | 是否保留 | 理由 |
| --- | --- | --- |
| `workspaces` | 保留 | 后续切多用户不需要 schema migration |
| `members` / `member_agent_scopes` | 保留（**仅种子 default owner**） | 同上 |
| `auth_tokens` | 保留（**仅 daemon runtime token**，无 UI 暴露） | 同上；`/ws` token gate 用此表 |
| `audit_log` | 保留（**Web 不暴露**，但 backend mutation 仍写） | 多用户切换时直接可用 |
| `squads` / `squad_members` | 保留（**仅 Phase B 评估是否暴露**） | in-process modes 已覆盖单用户场景 |
| `projects` / `project_repos` / `project_docs` | 保留（**仅 Phase B 评估是否暴露**） | 同上 |
| `members` / `tokens` router | 保留 | 同上 |
| `audit` / `vcs` router | 保留 | 同上 |

**Web UI 不渲染的元素**（在 `apps/web/app/[workspaceSlug]/(dashboard)/layout.tsx` 中通过路由级白名单控制）：

- `/members` → 不渲染入口（router 仍注册）
- `/audit` → 不渲染入口
- `/projects` → 不渲染入口（除非 Phase B 决策改）
- `/squads` → 不渲染入口（同上）
- workspace 切换器 → 不渲染

**单用户数据种子**（在 `orchestratord serve` 首次启动时执行）：

- 一个 `default` workspace（slug = `default`）
- 一个 owner member（id = 固定 UUID `00000000-0000-0000-0000-000000000001`）
- 一条 daemon runtime token（hash 存储，plaintext 仅一次性返回）

### 4.3 数据层已就位（无需迁移）

v2 周期开始时**已落地**的清单：

- 42 个 alembic migrations（`0001_create_tables.py` 至 `0042_index_integrations_workspace_provider.py`）
- DB 模型（`src/orchestratord/db/models/`）：`tenancy / agents / sessions / skills / inbox / collab / audit_auth / integrations / issues / vcs`
- 域模型（`src/orchestratord/domain/`）：`workspace / member / agent / runtime / session / issue / skill / inbox / channel / project / squad / audit / auth_token / autopilot / integration / usage / mention`
- 18 个 FastAPI router（`agents / audit / autopilots / channels / dashboard / inbox / integrations / issues / members / projects / realtime / runtimes / sessions / skills / squads / tokens / usage / vcs`，不含 `__init__.py`）
- WebSocket `/ws` 协议骨架（heartbeat + subscribe/unsubscribe + ack）

v2 周期**不重写**上述层，只在 Phase A–E 内填实现深度。

### 4.4 Runtime 接入（multica `daemon-runtimes` 对齐）

> **v2 周期新增章节**。multica 把"一台电脑 + 该电脑上的一款 AI 编程工具（或自定义运行时配置）"作为 first-class 抽象 —— **runtime**（守护进程文档 [`daemon-runtimes`](https://multica.ai/docs/zh/daemon-runtimes)）。runtime 与 agent 协议后端是两层独立抽象：协议后端决定"用什么 CLI 协议通信"，runtime 决定"在哪台机器、用哪条配置跑这个 CLI"。orchestratord 在协议层（SPI / entry-points / 26 SupportedTypes 对齐）已就绪，runtime/daemon 层几乎全缺；这是团队场景（多机器、多 workspace、wrapper 工具、固定版本 CLI）能否落地的关键。

**v2 周期起点**（已落地）：

- `src/orchestratord/domain/runtime.py`（87 行）：Runtime 实体 + 3 状态 enum（`ONLINE` / `OFFLINE` / `DISABLED`）+ token 合同（`issue_runtime_token` / `hash_runtime_token` / `verify_runtime_token`）
- `src/orchestratord/runtime/live_registry.py`（86 行）：进程内 registry
- `src/orchestratord/api/routers/runtimes.py`（149 行）：FastAPI 路由（含 token issue / heartbeat / 列表 / 详情）
- WS heartbeat 30s（§5.1）

**multica runtime 形态 / orchestratord v2 缺口**：

| 维度 | multica | orchestratord v2 | 缺口 |
| --- | --- | --- | --- |
| **守护进程启动 PATH 探测** | daemon 启动扫描 26 个 CLI；为有权 workspace 注册 runtime | ❌ 无（`install.sh` 一次性探测；无运行时重扫） | **严重** |
| **自定义运行时配置** | 创建时选协议族 + 固定参数（命令、引号转义、协议 flag `-p` / `--output-format` / `--input-format` / `--permission-mode` 剔除、模型覆盖、参数禁管道/重定向/`&&`/`;`/反引号/env 展开） | ❌ 无（仅 entry-points 机制） | **严重** |
| **Task env 集成契约（5 个不可覆盖）** | `MULTICA_TOKEN` / `MULTICA_TASK_ID` / `MULTICA_AGENT_ID` / `MULTICA_WORKSPACE_ID` / `MULTICA_SERVER_URL` —— agent 自定义环境无法覆盖 | ❌ 无 env 注入机制 | **严重** |
| **Task env 仅供参考（5+ 个）** | `MULTICA_TASK_CONFIG_ROOT` / `MULTICA_TASK_WORKSPACES_ROOT` / `MULTICA_AGENT_NAME` / `MULTICA_DAEMON_PORT` / `MULTICA_TASK_SLOT` / `TMPDIR` 等 | ❌ 无 | 中等 |
| **`workspaces_root` 三级覆盖** | flag > `MULTICA_WORKSPACES_ROOT` env > profile 配置；修改根目录不迁移已有执行目录 | ❌ 无 workspaces 根概念 | **严重** |
| **心跳 / 离线宽限期** | 15s 心跳；3 分钟内显示离线；7 天无 agent 绑定自动清理 | 30s WS heartbeat 骨架；无宽限期 / 自动清理 | 中等 |
| **并发上限** | daemon 全局默认 20 + 单 agent 默认 6（取较小）；env `MULTICA_DAEMON_MAX_CONCURRENT_TASKS` 可调 | ❌ 无运行时配额 | 中等 |
| **私有 / 公开 visibility** | 私有 = 仅 owner 能用其建 agent（admin 也不行）；公开 = 成员可路由但不分享登录凭据 | ❌ 无 visibility 概念（D9 单用户模式下不显现，但数据层需保留 `visibility` 字段） | 中等 |
| **离线排队恢复** | runtime 离线时 queue 不失败；宽限期满 + 仍排队满才失败 | ❌ 无 | 中等 |
| **Runtime UI** | Multica Desktop 列出 hostname + 在线状态 + 各 CLI 探测结果 + 自定义配置入口 | `packages/views/src/runtimes/runtimes-list.tsx` + `runtimes/{id}/page.tsx` 路由已占位但 stub | 中等（v2 §7.4 之后评估） |

**v2 周期取舍**：

| 子节 | 落地时机 | 范围 |
| --- | --- | --- |
| §4.4.1 **必做 / Phase A 同期** | daemon 启动 PATH 扫描（CLI 列表 → backend dispatch 映射）+ 5 个 Task env 集成契约注入（参考 multica `MULTICA_*` 命名空间）+ `workspaces_root` 三级覆盖 + daemon 全局并发默认 20（单 agent 并发走 `parallel_sessions` capability 已有机制） |
| §4.4.2 **必做 / Phase A 同期** | 自定义运行时配置（协议族 + 命令字段 + 引号 / 反斜杠转义 + 协议 flag 剔除 + 模型覆盖；参数语义限制同 multica）—— 这正是企业部署的核心场景；不补等于把"团队内部 wrapper / 固定版本可执行文件 / 兼容工具追加参数"挡在门外 |
| §4.4.3 **后置 / Phase D 同期** | 私有/公开 `visibility` 字段（单用户模式 D9 下不显现，但数据层 alembic migration 必加，便于多用户切换直接启用） |
| §4.4.4 **后置 / Phase C 同期** | 离线排队恢复 + 7 天自动清理（依赖 Phase C 调度器 + autopilot scheduler 落地） |

**§4.4 验收**：

- `orchestratord daemon start` 启动时打印"已探测到 N 个 CLI：claude, codex, dsh, ..."清单，缺哪个明示探测命令（multica `command -v <工具>` 风格）
- 新建 task 时 agent 子进程环境包含 `ORCHESTRATORD_TASK_ID` / `ORCHESTRATORD_WORKSPACE_ID` / `ORCHESTRATORD_AGENT_ID` / `ORCHESTRATORD_SERVER_URL` / `ORCHESTRATORD_TOKEN`（命名沿用 `ORCHESTRATORD_*` 与 multica `MULTICA_*` 区分；待讨论是否对齐）
- `orchestratord config set workspaces_root /var/lib/orchestratord/ws` 后新建 task 工作目录落在此根下；改根目录不迁移已有目录
- `orchestratord runtime profile create --family codex --command "/usr/local/bin/codex-wrapper --region cn"` 创建自定义 runtime，wrapper 启动后 multica 协议 flag（`-p` 等）由 daemon 注入而非 wrapper 硬编码
- 单台 daemon 同时跑满 20 个 task 后第 21 个进入排队（不入失败）

---

## 5. Phase A — Realtime + session control core

> v2 优先级最高的一组（D11）。本节是 v2 实施清单。

### 5.1 真 WebSocket pub/sub backbone

**当前**：`_ws_token_valid()` 仅校验非空 + 非 `bogus`；`subscribe/unsubscribe` 仅存 topic 集合；`session.approve` 只 ack。

**目标**：topic-based 转发 + 进程内 pub/sub。

**实现要点**：

- 在 `src/orchestratord/api/realtime.py` 抽出 `RealtimeBroker` 单例：`asyncio.Queue` 多生产者单消费者拓扑
- topic 形如 `issue.{id}` / `session.{id}` / `agent.{id}.capability` / `inbox.{workspace_id}`
- `BackendRunner` 在每次 `events()` 产出 `EventEnvelope` 时调用 `broker.publish(topic, payload)`
- WebSocket handler 维护 `set[str]` 已订阅 topic，**心跳 + 广播**使用长循环协程
- 单实例足够（multica `server/internal/realtime` 也是同结构）；多实例时再上 Redis pub/sub
- 协议（参考 multica `server/internal/realtime`）：

```jsonc
// server → client
{ "type": "event", "topic": "session.{id}", "payload": <EventEnvelope> }
{ "type": "inbox.created", "payload": {...} }
{ "type": "agent.capability.changed", "payload": {...} }

// client → server
{ "type": "subscribe", "topics": ["issue.123", "session.abc"] }
{ "type": "unsubscribe", "topics": ["issue.123"] }
{ "type": "session.approve", "session_id": "...", "tool_call_id": "..." }
```

**保留**：30s 心跳（同 multica `HeartbeatInterval`）；首次连接 0.5s liveness probe 后再 30s 节奏。

**验收**：3 个订阅者订阅同一 `session.{id}`，daemon 1 个 `TEXT_DELTA` 事件触达全部连接（fan-out）。

### 5.2 会话控制端到端（approve / deny / pause / resume / stop）

**当前**：router stub 仅 ack；`APPROVAL_REQUEST` 事件尚未流回 `BackendRunner`。

**目标**：用户点击 approve 后，daemon 端 `BackendRunner` 的 pending-approval waiter 被释放；backend capability 校验决定哪些按钮在 UI 渲染。

**实现要点**：

- 新增 `src/orchestratord/api/routers/sessions.py` 路由：
  - `POST /api/sessions/{id}/approve { tool_call_id, decision }` → 通过 `ipc` 调用 `BackendRunner.approve(session_id, tool_call_id)`
  - `POST /api/sessions/{id}/deny { tool_call_id, reason }` → 同上但传 deny
  - `POST /api/sessions/{id}/pause` → 仅在 `interrupt` capability 启用时存在
  - `POST /api/sessions/{id}/resume` → 同上
  - `POST /api/sessions/{id}/stop` → 同上
- 每条调用先查 `Session` + `BackendCapabilities`；capability 不支持时返回 409
- 调用结果经 `RealtimeBroker` 广播 `session.control.applied` topic
- Web UI：`<SessionDetail>` 收到 `APPROVAL_REQUEST` 事件渲染 approve/deny 按钮；点击走 `useSessionControl` mutation
- 多 agent 模式下，`decision` 还需要带 `proposer_id`（debate 模式独立思考约束）

**验收**：clawcodex session 触发 `APPROVAL_REQUEST`，Web UI 看到按钮；点 approve 后 daemon 端 waiter 释放，session 继续产出 `TEXT_DELTA`（端到端延迟 < 200ms 同区域）。

### 5.3 拖拽看板

**当前**：`apps/web/app/[workspaceSlug]/(dashboard)/issues/page.tsx` 渲染 `<IssuesBoard>`（含 list/kanban toggle），但 kanban 列间拖动未实装。

**目标**：用 `@dnd-kit/core` + `@dnd-kit/sortable` 实现拖拽 status 切换，乐观更新 + WS 回流校正。

**实现要点**：

- 新增 `packages/views/src/issues/kanban-column.tsx` + `kanban-card.tsx`
- `useIssueStatusChange` mutation 调用 `PATCH /issues/{id} { status }`
- 乐观更新：`queryClient.setQueryData(...)` 立刻反映；WS 回流 `event topic=issue.{id}` 携带最新 status 时覆盖乐观
- 失败回滚：HTTP 4xx/5xx → `queryClient.invalidateQueries` + toast
- DnD context：`<DndContext onDragEnd>` 监听列间落点

**验收**：把 issue 从 `pending` 拖到 `running`，UI 立即反映；daemon 在 100ms 内接收到 status 变更事件。

### 5.4 多 agent 模式可视化

**当前**：`event-timeline.tsx` 仅扁平时间轴。

**目标**：根据 `session.mode` 字段分派到不同渲染器。

**实现要点**（在 `packages/views/src/sessions/` 下新增）：

| 模式 | 渲染器 | 数据源 |
| --- | --- | --- |
| `single` | `event-timeline`（扁平） | `events[seq]` |
| `pipeline` | `pipeline-graph.tsx` —— 垂直链路 stage + 上下文注入标注 | `runs[stage_id].events[]` |
| `debate` | `debate-cards.tsx` —— 左/右 proposer 并列卡片 + judge 卡片 + 独立思考徽标 | `runs[proposer_id].events[]` |
| `swarm` | `swarm-tree.tsx` —— `task_decomposition.json` 渲染为 wave 树 + 完成/进行中/待办着色 | `swarm_checkpoint.json` |
| `coordinator` | `coordinator-gantt.tsx` —— 任务分发甘特图 | `runs[].events[]` 时间窗 |

- `SessionDetail` 顶部 `mode` 徽标 + 切换子视图
- 每个 renderer 只读 `events` + `runs`，**不**重复事件合并逻辑（合并在 `BackendRunner` 已做）

**验收**：在 `modes/pipeline` 测试场景下，`SessionDetail` 渲染 3 stage 链路，每个 stage 显示自己的 `TEXT_DELTA` 流。

### 5.5 工具调用卡片 + transcript 浮层

**当前**：`event-timeline` 通用渲染；无 4KB 截断、无 diff 着色、无 transcript 浮层。

**目标**：

- `TOOL_CALL` / `TOOL_RESULT` 渲染为折叠卡片，超过 4KB 时 `truncated: true` + "view full" 链接
- transcript 浮层：会话列表点 "transcript" 打开 dialog，呈现完整 timeline
- diff 着色：暂不做（multica `diff-highlight.ts` 复杂，Phase E）

**实现要点**：

- 新增 `packages/views/src/sessions/tool-call-card.tsx` + `tool-result-card.tsx`
- 新增 `packages/views/src/common/task-transcript/transcript-dialog.tsx`
- 4KB 截断：复用 `chat_gateway.py` 的 `_MAX_TOOL_RESULT_CHARS = 4096`

**验收**：session 触发 `TOOL_CALL("bash", "ls -la")` + `TOOL_RESULT(stdout=12KB)`，UI 渲染为卡片 + 截断提示。

### 5.6 Web provider tree 补全

**当前**：`apps/web/app/layout.tsx` 17 行；`DashboardGuard` 仅挂 `CoreProvider` + `I18nProvider`。

**目标**：补 `QueryClient` / `Theme` / `Auth` 三件套。

**实现要点**（`apps/web/app/web-providers.tsx`）：

```tsx
<QueryClientProvider client={queryClient}>
  <ThemeProvider attribute="data-theme" defaultTheme="dark">
    <AuthGate>
      <I18nProvider>
        {children}
      </I18nProvider>
    </AuthGate>
  </ThemeProvider>
</QueryClientProvider>
```

- `QueryClient`：复用 `packages/core/src/query-client.ts`
- `ThemeProvider`：`next-themes`（multica 已用）
- `AuthGate`：D9 单用户模式 → 单一 `dev` session（替代 §3.4 cookie session）。`AuthGate` 仅渲染 default member identity，**不**渲染登录页
- 登录页 `apps/web/app/login/page.tsx`：单用户模式下重定向到 `/default`

### 5.7 Phase A 验收

| 项 | 验收产物 |
| --- | --- |
| §5.1 WebSocket pub/sub | 3 个浏览器订阅同一 session，daemon 1 个事件 fan-out 全部可见 |
| §5.2 会话控制 | clawcodex session `APPROVAL_REQUEST` 端到端 approve < 200ms |
| §5.3 拖拽看板 | kanban 拖动 issue 改 status 乐观更新 + WS 校正 |
| §5.4 多 agent 模式 | pipeline / debate / swarm / coordinator / single 5 个模式各自渲染器至少跑通 1 个测试场景 |
| §5.5 工具调用卡片 | 4KB 截断 + transcript 浮层可用 |
| §5.6 Provider tree | QueryClient / ThemeProvider / AuthGate / I18nProvider 全栈接通 |

---

## 6. Phase B — 通讯层（chat + Slack/Lark）

### 6.1 Workspace-level chat

> **状态（2026-09-08）**：主链路已落地——chat 页 + `POST /api/workspaces/{ws}/chat/sessions`（创建 pending 会话 + 初始消息，daemon 认领后跑真实 BackendRunner）+ messages 端点 + `chat_bridge` 折叠落库；流式推送已闭环——bridge sink 发布 `chat.{session_id}` 帧（终结帧在 commit 后发），前端订阅 + 流式缓冲气泡 + `chat.` 失效映射（§0.5 表 #3，无 socket 时回落 2s 轮询）。

- 新增 `apps/web/app/[workspaceSlug]/(dashboard)/chat/page.tsx` + `packages/views/src/chat/`
- 不创建 issue 也能发 prompt → 启动 session
- 数据模型：复用 `sessions` 表（不带 `issue_id`）+ `messages` 表（新增 migration）
- 复用 `RealtimeBroker`（§5.1）做流式推送

### 6.2 Mention 路由

> **状态（2026-09-08）**：mention 路由 + 文本解析均已落地——显式 `agent_id`/`member_id` 优先，都为空时 `parse_mentions` 逐 handle 查 `agents.by_name` / `members.by_name`（agent 优先、按出现顺序首个命中），解析不出维持 422（§0.5 表 #2）。

- `parse_mentions()` 已写（`src/orchestratord/domain/mention.py`）
- 在 `/api/workspaces/{ws}/issues/{id}/mention` 接 `BackendRunner`：根据 `agent_id` / `member_id` 启动 session
- member mention 在单用户模式下等价于 self-mention

### 6.3 Slack / Lark OAuth handshake

**当前**：`integrations` router 接收 `webhook_url` 字符串存储；无 OAuth 流程。

**目标**：标准 OAuth 2.0 授权码 + 状态校验 + token exchange。

**实现要点**：

- 新增 `orchestratord.integrations.oauth` 子模块：`SlackOAuth` / `LarkOAuth` 各自实现 `authorize_url()` / `exchange_code()` / `refresh_token()`
- `GET /api/workspaces/{ws}/integrations/slack/authorize` → 重定向到 Slack OAuth
- `GET /api/workspaces/{ws}/integrations/slack/callback?code=&state=` → `exchange_code()` + 写 `integrations` 表 + 通知 `channels` router
- `client_id` / `client_secret` 从环境变量读（`ORCHESTRATORD_SLACK_CLIENT_ID` 等）

### 6.4 Inbound webhook

- `POST /api/integrations/slack/events` 接 Slack Events API（URL verification + event_callback）
- `event.text` 含 `@orchestratord <text>` → 复用 `channels.trigger()`（已写）
- 单用户模式：`channel_id` 通过 `external_id` 反查

### 6.5 多 VCS

- **GitHub**：`vcs.py` router 已写（installations + pull_requests + webhook），仅需补 GitHub App 真实 OAuth handshake（与 §6.3 同构）
- GitLab / Gitea / Forgejo：后置；v2 不补

### 6.6 Phase B 验收

> **状态（2026-09-08）**：① 落库 ✓ / 流式接收 ✓（§0.5 表 #3 闭环）；② 文本解析 ✓（§0.5 表 #2 闭环）；③ Slack OAuth authorize / callback / events 路由与实现均在（未活体验收）。

- workspace-level chat 跑通：发 prompt → 流式接收 → 落库
- mention `@agent-name` 触发 session
- Slack OAuth authorize → callback → integration 落库，webhook 接收事件

---

## 7. Phase C — 调度与可观测

### 7.1 Autopilot scheduler loop

> **状态（2026-09-08）**：调度器已完整实现（`src/orchestratord/scheduler/autopilot.py`，asyncio task + croniter + start/stop/tick/claim_slot/mark_failed，tests/scheduler 全绿），`serve` lifespan 已启动（`api/app.py:78-89`，`ORCHESTRATORD_AUTOPILOT_DAEMON=1` 门控）；独立 daemon（`orchestratord server start`）路径也已同参启动（§0.5 表 #4）。

**当前**：CRUD 已写；无调度循环。

**目标**：asyncio task + croniter（不引入 APScheduler 重依赖），周期触发 workflow。

**实现要点**：

- 新增 `src/orchestratord/scheduler/autopilot.py`：
  - `class AutopilotScheduler`：`asyncio.create_task(self._loop())`
  - 每分钟轮询 `enabled=True` 的 autopilot → 计算下次触发时间 → 写 `autopilot_runs` → 启动 workflow
- `orchestratord serve` 启动时 `await scheduler.start()`
- 优雅关闭：`SIGTERM` 时 `await scheduler.stop()`

### 7.2 Token cost 估算

> **状态（2026-09-08）**：估算器已实现于 `src/orchestratord/cost/estimator.py`（`load_pricing` 读 `packages/core/src/pricing/pricing.json`，`estimate_cost_usd(model, tokens_in, tokens_out)`；精确 id → 最长前缀 → default 匹配），`POST /api/workspaces/{ws}/usage` 在 `cost_usd==0` 时调用；`BackendRunner` 的 run 完成路径也经同一估算器兜底（§0.5 表 #1）。

- `cost_reporting=False` 的 backend 走 token estimator：`@orchestratord/cost/estimator.py`
- 输入：token 数 + 模型；输出：USD
- 模型价格表：`pricing.json`（在 `packages/core/src/pricing/`）

### 7.3 Usage 聚合 + 图表

> **状态（2026-09-08）**：`usage_aggregates` 表 + 读侧聚合（`domain/usage.py`）+ `usage-charts.tsx`（折线/柱状，含 `cost_usd`）+ usage 路由均已落地；生产写入路径已闭环——`BackendRunner._record_usage()` 在 run 完成后 best-effort upsert（`UsageAggregateRepository.upsert` ON CONFLICT；`chat_daemon` 经 `task.context` 传 workspace/agent/issue id）（§0.5 表 #1）。

- `usage_aggregates` 表已建
- 新增 `packages/views/src/usage/usage-charts.tsx`（折线 + 柱状）

### 7.4 Inbox 详情

- `inbox-list.tsx` 单文件 stub
- 拆 `inbox/{clarification,approval,failure}` 三个子组件

### 7.5 Phase C 验收

> **状态（2026-09-08）**：三条全部满足——① serve 启动 + tests/scheduler 全绿；② run 完成写入 `usage_aggregates` 已接线（§0.5 表 #1）；③ 三类卡片组件在。

- autopilot 每 5 分钟触发一次，产 issue + 落 run
- 无 `cost_reporting` 的 backend 在 usage 页有估算 USD
- inbox 三类事件各自差异化视图

---

## 8. Phase D — 后端协议覆盖

### 8.1 已落地 backend 现状（v2 实测，2026-09-08 复测修正）

> Phase D 三件事（§8.2）已全部落地；下表 LOC / 缺口列按当前代码复测修正。

| backend | 真实深度 | 缺口 |
| --- | --- | --- |
| clawcodex (1653 LOC) | 完整 SDK worker + approval | — |
| dsh (1440 LOC) | SDK 包装 + agent.cordis 补丁 | — |
| codex (987 LOC) | app-server + Cli 双路径 | — |
| claude (631 LOC) | SDK 包装 | — |
| opencode (1094 LOC) | 真实 /api 协议传输重写（1f2a929） | — |
| qwen (441 LOC) | stream-json | — |
| hermes (220 LOC) | 简单 CLI | — |
| copilot (623) / cursor (815) / kimi (1084) / reasonix (1247) / zeroclaw (961) | 真 session 翻译（2026-09-08 已翻正） | — |
| openclaw (302 LOC) | **stub**（仅 descriptor） | 后置 |
| kiro-cli (299 LOC) | `backend.py` + `session.py` 已补 | — |
| orchestratord-acp (811 LOC) | 通用 ACP 包已落地（导出 codebuddy / deveco / qodercli / qoderclicn descriptor） | — |

### 8.2 Phase D 三件事

> **状态（2026-09-08）**：三件事全部落地——① `orchestratord-acp` 通用包导出 codebuddy / deveco / qodercli / qoderclicn 四个 descriptor（qwenpaw 未导出，后置）；② `protocol_family` / `runtime_id` 两层派生在 `spi/backend_descriptor.py` + `backend_registry.py`；③ 5 个 stub 已真翻译（LOC 见 §8.1）。

1. **落地 `orchestratord-acp` 通用包**（草案 §8.3）：
   - 实现通用 ACP backend（基于 `@agentclientprotocol/sdk` 或自写 JSON-RPC stdio）
   - 优先覆盖 3 个高频 ACP backend：codebuddy / deveco / qoderclicn
2. **`protocol_family` / `runtime_id` 分离**（草案 §8.4）：
   - `spi/backend_descriptor.py` 新增 `protocol_family` 字段
   - `backend_registry._classify_family()` 同时支持 id 与 family 两层
   - 允许 `omp` → `pi` 这类 builtin runtime 派生
3. **5 个 stub backend 真翻译**：
   - copilot / cursor / kimi / reasonix / zeroclaw 各自补真 session 翻译（参照 multica 各 `_invocation.go` 拆分）

### 8.3 multica 完整 26 family 全清单与 v2 覆盖决策

> **状态（2026-09-08）**：下表 🟡 行（#3 copilot、#4 cursor、#8 kimi、#10 reasonix、#11 zeroclaw）已全部翻正为 ✅（真翻译）；#22 kiro 已补齐（orchestratord-kiro-cli 299 LOC）；#15–#17 经 ACP 通用包覆盖；#5 opencode 已重写（594→1094 LOC）。⚪ 后置行维持不变。

multica `SupportedTypes`（`server/pkg/agent/agent.go:312`）列出的 26 个 protocol family，加 1 个 builtin runtime 派生（`omp` → `pi` family）。下表给出 v2 周期内 orchestrator / Phase D / 后置的三档决策：

| # | family | multica 文件 | v2 状态 |
| --- | --- | --- | --- |
| 1 | `claude` | `claude.go` + 4 个 _test | ✅ orchestratord-claude（631 LOC）已硬实现 |
| 2 | `codex` | `codex.go` + cleanup_unix_test | ✅ orchestratord-codex（987 LOC）app-server + Cli 双路径 |
| 3 | `copilot` | `copilot.go` + `_invocation.go` + windows 分支 | 🟡 Phase D 补真翻译（stub 296 LOC） |
| 4 | `cursor` | `cursor.go` + 4 个 _test | 🟡 Phase D 补真翻译（stub 296 LOC） |
| 5 | `opencode` | `opencode.go` + session | ✅ orchestratord-opencode（594 LOC）SSE 翻译 |
| 6 | `dsh` | `dsh.go` | ✅ orchestratord-dsh（1440 LOC）SDK + agent.cordis 补丁 |
| 7 | `hermes` | `hermes.go` | ✅ orchestratord-hermes（220 LOC）简单 CLI |
| 8 | `kimi` | `kimi.go` | 🟡 Phase D 补真翻译（stub 297 LOC） |
| 9 | `qwen` | `qwen.go` + windows 分支 | ✅ orchestratord-qwen（441 LOC）stream-json |
| 10 | `reasonix` | `reasonix.go` + 3 个 _test | 🟡 Phase D 补真翻译（stub 300 LOC） |
| 11 | `zeroclaw` | `zeroclaw.go` + 840 行 _test | 🟡 Phase D 补真翻译（stub 300 LOC） |
| 12 | `openclaw` | `openclaw.go` | ⚪ 后置（stub 302 LOC） |
| 13 | `pi` | `pi.go` + session_lock + stdin + _test | ⚪ 后置（multica 完整 builtin，v2 不补） |
| 14 | `omp`（builtin runtime）| `builtin_runtimes.go` 派生 `pi` | ⚪ 后置（v2 protocol_family 落地后开路） |
| 15 | `codebuddy` | `codebuddy.go` + discovery fallback | 🔵 ACP 通用包覆盖 |
| 16 | `deveco` | `deveco.go` | 🔵 ACP 通用包覆盖 |
| 17 | `qoder` / `qoderclicn` | `qoder.go` (449) + 1041 行 _test | 🔵 ACP 通用包覆盖（qoderclicn 优先） |
| 18 | `qwenpaw` | `qwenpaw.go` (369) + integration _test | 🔵 ACP 通用包覆盖（per-task workspace） |
| 19 | `antigravity (agy)` | `antigravity.go` + _test | ⚪ 后置（私有协议） |
| 20 | `codearts` | `codearts.go` + cancel/integration _test | ⚪ 后置（私有协议 + Windows 取消特殊） |
| 21 | `grok` | `grok.go` | ⚪ 后置 |
| 22 | `kiro` | `kiro.go` | ⚪ 后置（orchestratord-kiro-cli 仅 pyproject） |
| 23 | `mcode` | `mcode.go` | ⚪ 后置（私有协议） |
| 24 | `dim` | `dim.go` | ⚪ 后置（私有协议） |
| 25 | `traecli` | `traecli.go` (448) + integration _test | ⚪ 后置（私有协议） |

**说明**（2026-09-08 更新）：multica 完整 26 family 中，orchestrator 已覆盖 16 个——v2 首批 6 个（claude / codex / opencode / dsh / hermes / qwen）+ Phase D 补齐 5 个真翻译（copilot / cursor / kimi / reasonix / zeroclaw）+ kiro-cli（299 LOC）+ ACP 通用包覆盖 codebuddy / deveco / qoder / qoderclicn 4 个。后置 10 个（openclaw / pi / omp / qwenpaw / antigravity / codearts / grok / mcode / dim / traecli——私有协议 / Windows 特殊 / multica 完整 builtin 派生；qwenpaw 的 ACP 抽象已就绪、descriptor 未导出）。

### 8.4 协议基础设施后置清单（multica 已沉淀 / orchestratord 缺）

multica 在 `server/pkg/agent/` 已沉淀 9 项跨 family 的协议基础设施。v2 仅补 ACP（§8.2.1），其余 8 项列入后置清单：

| 抽象 | multica 文件 | orchestratord 状态 | 后置阶段 |
| --- | --- | --- | --- |
| ACP deliverable / effort / terminal / usage | `acp_{deliverable,effort,terminal,usage}.go` | §8.2.1 落地 | — |
| Stream-JSON 通用解析 | `stream_json_result.go` + `stream_scanner.go` + `stream_json_final_output_test.go` | 无（每 backend 各自解析） | Phase D 后置 |
| app-server JSON-RPC 通用适配 | `codex.go` 复用 codex app-server | `orchestratord-codex` 自有，未抽 | Phase D 后置 |
| session lock（POSIX / Windows） | `pi_session_lock_unix.go` / `_windows.go` | 无 | 后置（多 backend 落地后） |
| proc 组管理（POSIX / Windows） | `proc_windows.go` (295) / `proc_other.go` (77) | 仅 clawcodex / dsh 各自实现 | 后置（统一 cancel 接口） |
| thinking 抽象 | `thinking.go` (999) | 无 | 后置 |
| run_collect 生命周期 | `run_collect.go` (501) + `run_collect_quiet.go` + lifecycle _test | 无（每 backend 自定义 SESSION_COMPLETE） | 后置 |
| version 协商 | `version.go` (178) + _test | 仅 `clawcodex` 有版本断言 | 后置 |
| Browser MCP 配置注入 | `browser_mcp_config.go` | 无 | 后置 |

**单一架构原则**：ACP 通用包落地后，仍在每个 backend 里散落的协议适配会让"协议位能否补齐"的成本无法摊薄。8 项后置清单按依赖关系排序：stream-json → app-server → proc 组 → run_collect → thinking → version → session lock → browser MCP。

### 8.5 Phase D 验收

> **状态（2026-09-08）**：三条全部满足（见 §8.1 / §8.2 状态注记）。

- ACP 通用包至少覆盖 3 个 backend（codebuddy / deveco / qoderclicn）
- 5 个 stub backend 有真 session 翻译（不再仅 ack）
- `protocol_family` / `runtime_id` 字段在 `BackendDescriptor` 落地

---

## 9. Phase E — 文档站深耕 + i18n + 测试烟囱

> **状态（2026-09-08）**：`apps/docs` Fumadocs 骨架已落地；i18n 三语（en / zh-CN / ja）locales 已在；`agentintegration` 测试烟囱已就位（pyproject `addopts = "-m 'not agentintegration'"`，显式 opt-in）。**待补**：§9.1 的中英双语 `conventions.mdx` 术语对照内容深度。

### 9.1 文档站

- `apps/docs` 已 Fumadocs 骨架，补内容深度（中英双语 `conventions.mdx` 术语对照表）

### 9.2 i18n

- 当前 `packages/views/src/i18n/locales/{en,zh-CN}.ts` 两份小字典
- 扩到三语：`en` / `zh-CN` / `ja`

### 9.3 测试烟囱

- 真 agent CLI 烟雾测试走 `agentintegration` build tag（同 multica）
- CI 默认 **绝不** resolve / execute 用户安装的 agent CLI
- `scripts/agent-cli-command-names.txt` 锁住 daemon 默认命令清单

---

## 10. 兼容性 / 迁移策略

### 10.1 CLI 兼容

- 保留所有现有 `orchestratord` 子命令语义
- `dashboard` 子命令保留 ≥ 1 个 release cycle（§3.6）
- `run` / `workflow` / `skills` / `backend` / `app` 子命令不变
- 新增 `serve` / `web` 子命令（不替换 `dashboard`）

### 10.2 单用户模式下的数据种子

- `orchestratord serve` 首次启动时执行 seed：
  - 1 个 `default` workspace
  - 1 个 owner member（UUID 固定）
  - 1 条 daemon runtime token（hash 存储，plaintext 仅一次性返回）

### 10.3 多用户切换路径（未来）

- 数据层已就位（§4.2）；切换多用户只需：
  - 在 `apps/web/app/[workspaceSlug]/(dashboard)/layout.tsx` 恢复 workspace 切换器
  - 解开 `apps/web/app/login/page.tsx` 的重定向
  - 在 `apps/web` 渲染 `/members` `/tokens` `/audit` `/projects` `/squads` 入口
- 不需要数据迁移

### 10.4 数据库迁移

- 旧磁盘事件日志：一次性脚本 `scripts/migrate_eventlog_to_db.py` 导入（草案 §10.2）
- 单工作区用户：自动迁移到 default workspace
- 多工作区用户：手工创建 + 导入

### 10.5 后端包兼容

- 现有 15 个 backend 包**保持 ABI 兼容**
- 新增 `agent_capabilities_cache` 字段对老 backend 无影响（启动时探测失败也能启动）
- 测试守门：`tests/test_capability_drift.py` 任何 backend 改动必须通过 CI

### 10.6 Skills 兼容

- `SKILL.md` + `references/source-map.md` 格式不变
- `skills verify` CLI 保留
- Web 上 `/api/skills/{name}/source-map` 仅做只读视图

### 10.7 部署兼容

- `install.sh` 加 `--with-web` / `--with-db` flag
- 默认 `--no-web --no-db`（开发者模式，与现状一致）
- `--all` = 全部启用

---

## 11. 技术栈选型建议

### 11.1 后端

| 选择 | 理由 |
| --- | --- |
| FastAPI | 与现有 Python 一致；原生 OpenAPI + WebSocket + SSE |
| SQLAlchemy 2.x | 异步支持成熟；与现有 `pydantic` 数据契约对接 |
| Alembic | 与 FastAPI 集成；支持 non-transactional migrations（multica 风格） |
| asyncpg | 高性能 PostgreSQL 驱动；**`asyncpg<0.30.0`**（项目硬约束） |
| Redis | 多实例部署时 WS 中继；单实例可省 |
| croniter | autopilot 调度（不引入 APScheduler 重依赖） |

### 11.2 前端

| 选择 | 理由 |
| --- | --- |
| Next.js 16 App Router | 与 multica 一致；server components 减轻 Web 渲染压力 |
| TanStack Query | server state 唯一来源 |
| Zustand | client state；与 multica 一致 |
| shadcn + Radix | 不引入商业 license |
| @tanstack/react-table | DataTable 基础 |
| Tailwind CSS | 与 multica 一致；CSS 变量语义化 |
| react-flow | pipeline / debate / swarm / coordinator 模式可视化 |
| @dnd-kit | 拖拽看板 |
| next-themes | ThemeProvider（multica 已用） |

### 11.3 工程化

| 选择 | 理由 |
| --- | --- |
| pnpm | 与 multica 一致；与 Python `uv` 不冲突 |
| Vitest | TS 单元 / 组件 |
| pytest | Python 单元 / 集成（含 `tests/contracts/` T1–T9 + `test_capability_drift.py` + `test_backend_cli_guard.py`） |
| ruff | Python lint（已有） |
| ESLint + Prettier | TS lint |
| GitHub Actions | CI |

---

## 12. 风险与权衡

### 12.1 架构层面

| 风险 | 缓解 |
| --- | --- |
| 单用户模式默认 + 数据层多租户可能让初次接触者困惑 | README + ONBOARDING 明确"数据层为多用户预留，UI 仅单用户" |
| WebSocket pub/sub backbone 单实例瓶颈 | v2 不引入 Redis；多实例时再加 |
| daemon token 在单用户模式下泄露面更大 | token 仍然单向 hash；rotate 流程 + audit 保留 |
| 旧 LiveView 与 Web 长期共存 | 1 release cycle 后 dashboard 子命令 deprecated；之后彻底移除 |

### 12.2 产品层面

| 风险 | 缓解 |
| --- | --- |
| 拖拽看板 + 多 agent 模式可视化是 UX 重投入 | Phase A 内拆 PR；先做 single + pipeline，跑通再扩 |
| Slack/Lark OAuth 状态校验与 state token 表 | 复用 `audit_log`（D10 数据层保留） |
| stub backend 真翻译工作量不均 | 5 个 stub 各拆 PR；按使用频率排序：zeroclaw / copilot / cursor / kimi / reasonix |
| ACP 通用包协议复杂度 | 仅覆盖 3 个高频 backend（codebuddy / deveco / qoderclicn），其余 stub backend 各走自己的 protocol family |

### 12.3 安全层面

| 风险 | 缓解 |
| --- | --- |
| daemon token 泄露 | token 单向 hash 存储；rotate 流程 + 审计日志 |
| 用户安装的 agent CLI 被 Web 直连 | 物理隔离：agent CLI 仅 daemon 中转；server 不接受 agent CLI 直连 |
| 单用户数据越权（虽然无多用户，但防止环境逃逸） | 严守 `X-Workspace-ID` 头 + `workspace_id` 过滤 |
| Skills source-map 漂移被忽略 | `skills verify` 在 daemon 启动时强制执行；CI 守门；Web 端显示 verified 状态 |

### 12.4 工程纪律

| 风险 | 缓解 |
| --- | --- |
| 默认测试真实调用 agent CLI | 复用 multica 的 `agentintegration` build tag + 默认不可 resolve |
| backend 数量增加后 install.sh 探测表膨胀 | 探测逻辑统一（`detect_runtime` helper） |
| 与 clawcodex 的 strangler-fig 迁移未完成 | 严守 §3.1；新 Web 不绕过 `orchestratord` 直接调 backend 私有协议 |

---

## 13. 验收标准

### 13.1 功能验收

| 阶段 | 验收产物 |
| --- | --- |
| **Phase A** | 真 WebSocket pub/sub；端到端会话控制；拖拽看板；5 个模式可视化；工具调用卡片；provider tree 全栈 |
| Phase B | chat 域；mention 路由；Slack/Lark OAuth + inbound webhook |
| Phase C | autopilot scheduler；cost estimator；usage 图表；inbox 三类视图 |
| Phase D | ACP 通用包覆盖 ≥ 3 backend；5 个 stub backend 真翻译；`protocol_family` 落地 |
| Phase E | 文档站中英双语；i18n 三语；测试烟囱守门 |

### 13.2 性能验收

- Web 首屏 (Lighthouse)：TTI < 2s（中等规模工作区）
- WS 事件端到端延迟（daemon → 浏览器）：< 200ms（同区域）
- 1k events/s 单实例 server 不丢帧（基线压测）

### 13.3 工程纪律验收

- 所有 Python 测试通过（含 `tests/test_capability_drift.py`、`tests/test_backend_cli_guard.py`、`tests/contracts/`）
- 所有 TS 测试通过
- Playwright 关键路径通过
- ruff / ESLint 无 error
- OpenAPI 自动生成且 schema 覆盖所有 router
- 默认测试**绝无** resolve / execute 真实 agent CLI（守门验证）

### 13.4 安全 / 合规验收

- 单用户数据隔离：default workspace 数据不被外部越权
- audit_log 覆盖所有 mutation（即使 UI 不暴露）
- daemon token 不可逆 hash 存储
- agent CLI 仅 daemon 中转，server 无直连入口

---

## 14. 附录

### 14.1 关键文件路径索引

| 模块 | 路径 |
| --- | --- |
| SPI | `src/orchestratord/spi/` |
| Capabilities / Degradation | `src/orchestratord/spi/capabilities.py`、`spi/degradation.py` |
| Backend registry | `src/orchestratord/backend_registry.py` |
| Modes | `src/orchestratord/modes/` |
| Skills | `src/orchestratord/skills/` |
| LiveView (compat) | `src/orchestratord/cli/dashboard.py` |
| Status (TUI) | `src/orchestratord/status_dashboard.py` |
| Workflow engine | `src/orchestratord/workflow_engine/` |
| Backend packages | `backends/orchestratord-{clawcodex,claude,codex,dsh,hermes,opencode,copilot,cursor,kimi,qwen,kiro-cli,openclaw,reasonix,zeroclaw,acp}/` |
| FastAPI app | `src/orchestratord/api/app.py` |
| API routers | `src/orchestratord/api/routers/*.py` |
| DB models | `src/orchestratord/db/models/` |
| Domain models | `src/orchestratord/domain/` |
| Migrations | `alembic/versions/0001_*.py` ~ `0042_*.py` |
| Install | `install.sh` |
| Web app | `apps/web/` |
| Views | `packages/views/src/` |
| Core (TS) | `packages/core/src/` |
| UI | `packages/ui/src/` |
| Docs | `apps/docs/` |

### 14.2 参考来源（multica）

| 概念 | multica 路径 |
| --- | --- |
| 路由约定 | `apps/web/app/[workspaceSlug]/(dashboard)/` |
| 共享包分层 | `packages/{core,ui,views}` |
| State 规则 | `CLAUDE.md` "State Rules" |
| API 兼容性 | `CLAUDE.md` "API Compatibility" |
| 后端 UUID 规则 | `CLAUDE.md` "Backend UUID Rules" |
| Web/Desktop 共享 | `CLAUDE.md` "Sharing Rules" |
| DB 迁移规则 | `CLAUDE.md` "Database and Migration Rules" |
| 26 个 runtime | `scripts/agent-cli-command-names.txt` + `server/pkg/agent/agent.go` `SupportedTypes` |
| BuiltinRuntime | `server/pkg/agent/builtin_runtimes.go` |
| Realtime | `server/internal/realtime/` |
| VCS | `server/internal/integrations/` |
| 通知渠道 | `apps/web/app/{slack,lark,dingtalk,wecom,telegram}/` |
| i18n 规范 | `apps/docs/content/docs/developers/conventions.mdx` |
| ACP 抽象 | `server/pkg/agent/acp_{deliverable,effort,terminal,usage}.go` |
| Stream-JSON | `server/pkg/agent/stream_json_result.go` + `stream_scanner.go` |
| Proc 组管理 | `server/pkg/agent/proc_{windows,other}.go` |
| Run lifecycle | `server/pkg/agent/run_collect.go` |

### 14.3 配套文档

| 文档 | 内容 |
| --- | --- |
| `docs/FEATURE_GAP_VS_MULTICA_DETAILED.md` | v2 同期实测对比（文件 / 行数 / 路由 / backend 协议族覆盖率） |
| `docs/FEATURE_UNIFIED_CONVERSATION_ID.md` | 跨后端 conversation 同一性方案 |
| `DESIGN_backends_hardening.md` | backend 协议族深化（含 dsh / codex / opencode / hermes） |

---

**变更记录**

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-09-04 | 初稿；范围 §1.1–§1.2 划定；Phase 0–5 路线图 |
| v2 | 2026-09-07 | 同步实测基线；新增 D9 单用户模式 / D10 数据层保留 / D11 Phase A 优先；Phase 0–2 标记"已落地"；Phase 3–5 重新拆分为 Phase A–E；新增 §4 单用户模式决策、§5 Phase A 详化、§5.1–§5.7 实施清单；删除原 §5.7 多工作区章节（v2 不做） |