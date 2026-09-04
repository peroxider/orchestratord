# orchestratord vs multica — 特性缺口开发文档

> 状态：草案 v1  
> 作者：orchestratord 团队  
> 范围：在保留 orchestratord 现有架构优势（能力矩阵 / SPI / agent-callable Skills / in-process 多 agent 模式）的前提下，补齐 multica 已成熟的产品化形态。客户端形态仅补 Web，Desktop 与 Mobile 暂不实现。

---

## 0. 元信息

### 0.1 文档目的

orchestratord 已完成 M0–M5，6 个后端（clawcodex / claude / codex / dsh / hermes / opencode），463 测试通过；但当前形态是"开发者向的 Python 编排内核"，缺少产品化外壳。multica 是"产品向的 AI 任务管理平台"，完整支撑团队协作。本文档定义**如何把 multica 的产品形态以 Web 客户端的方式嫁接到 orchestratord 上**，同时**严格保留 orchestratord 的架构优势**。

### 0.2 与既有文档的关系

- 本文档**不替代** 既有 SPI 设计（已冻结的能力位 / 5-family taxonomy）
- 本文档**是** 产品层补充，专门解决"如何让团队用户在不放弃底层架构优势的前提下使用 orchestratord"
- 涉及的 backend 覆盖（缺口 20 个）会与 `DESIGN_backends_hardening.md` 中的 dsh / codex / opencode / hermes 演进保持锁步

### 0.3 同期决策

**D5 — Web 客户端形态选型**：仅 Web，不补 Desktop / Mobile（社区自建或后续评估）。  
**D6 — 数据库选型**：PostgreSQL 17（与 multica 同主版本，复用生态：`pgcrypto`、`pg_trgm`）。  
**D7 — Web 后端框架**：FastAPI（与现有 Python 一致，原生 OpenAPI + WebSocket + SSE）。  
**D8 — Web 前端框架**：Next.js 16 App Router + TanStack Query + Zustand（与 multica 共享心智模型，但不分 desktop / mobile 包）。  

---

## 1. 范围与非目标

### 1.1 In scope

| 项 | 内容 |
| --- | --- |
| Web 客户端 | Next.js 16 单端应用，含完整路由组与设计系统 |
| Web 后端 API | FastAPI app，复用现有 Python 模块（daemon / workflow / SPI / skills），不重写 |
| 持久化 | PostgreSQL 17 引入；事件日志 + run/session/event 三表起步 |
| 实时 | WebSocket 为主，SSE 保留为单用户 / 内网降级 |
| 多租户 | workspaces + member / agent assignee + 角色（owner/admin/member） |
| 协作层 | squads / projects / skills / autopilots / mentions 的后端实体 + Web 页面 |
| 后端覆盖 | 把 6 个后端补到 12 个（再补 6 个最常用的） |
| VCS 集成 | GitHub / GitLab（multica 的 Gitea / Forgejo 留作后置） |
| 通知渠道 | Slack / Lark 适配器；DingTalk / WeCom / Telegram 留作后续 |
| 文档 | Fumadocs 风格的 Web 文档站点（中英双语） |

### 1.2 Out of scope（明确不做）

| 项 | 不做的理由 |
| --- | --- |
| Desktop 客户端 | D5；electron-builder 与平台签名不在本期投入产出比之内 |
| Mobile 客户端 | D5；Expo + App Store 发布链路另立专项 |
| SaaS / 商业化 | 仅做自托管；计费 / 配额不实现，留接口（entitlement 表预留） |
| 全部 20 个剩余 CLI | 只补 6 个最常用；剩下按"每个 backend 一个 PR"的节奏滚动 |
| 自定义 runtime profile | Multica 的 `runtime_profile.protocol_family` 自定义白名单功能本期不做，复用 `SupportedTypes` 内置集合 |
| 多 VCS 后端 | GitHub / GitLab 二选一，先做 GitHub |
| 通知渠道 ≥ 3 个 | Slack / Lark 起步；DingTalk / WeCom / Telegram 后置 |

---

## 2. 缺口总览

| 维度 | 当前 orchestratord | multica 形态 | 缺口 |
| --- | --- | --- | --- |
| Web 客户端 | 单文件嵌入式 LiveView（`cli/dashboard.py`，2306 行） | Next.js 16 完整应用 | 严重 |
| 多客户端形态 | 单进程同端口 | Web + Desktop + Mobile | 严重（Web 优先） |
| 多租户 | 单进程单工作区 | workspaces + 角色 + access scopes | 严重 |
| 持久化 | 事件日志 + control socket | PostgreSQL 17 + Redis relay | 严重 |
| 实时 | SSE 单向 | WebSocket 双向 | 半缺 |
| 后端覆盖 | 6 个 | 26 个 | 中等 |
| 协作抽象 | 5 个 in-process 模式 | Squads / Projects / Autopilots | 中等 |
| 多 VCS | 单一 issue→PR（Linear） | GitHub / GitLab / Gitea / Forgejo | 中等 |
| 通知渠道 | 通用 HTTP 桥 | Slack / Lark / DingTalk / WeCom / Telegram | 中等 |
| 计费 / 配额 | 无 | 完整 SaaS | 不做（D5） |
| 文档站点 | `*.md` | Fumadocs + i18n | 半缺 |

详细差距见 §5（Web）、§6（后端）、§7（协作）、§8（后端覆盖）。

---

## 3. 必须保留的 orchestratord 优势特性

> 这一节是约束，不是建议。任何"补 multica 的形态"不得削弱下列属性。

### 3.1 SPI 与插件机制

保留对象：

- `src/orchestratord/spi/` 下的 `AgentBackend`、`AgentSession`、`BackendCapabilities`、`EventEnvelope`、`ApprovalPolicy` 五个文件
- `importlib.metadata` 解析 `orchestratord.backends` + `orchestratord.backend_descriptors` 双层 entry point
- CI 强制 core 不直接 import 任何 backend 包

Web 层不能绕过 SPI：所有 agent 交互必须经过 `BackendRunner` → `AgentSession.events()` 流；不允许 Web 直接 fork agent 进程或读 backend 私有协议。

### 3.2 能力矩阵 + 中央强制降级

保留对象：

- `spi/capabilities.py` 的 8 位能力（`streaming_deltas` / `resumable` / `interrupt` / `approval_hooks` / `parallel_sessions` / `cost_reporting` / `tool_filtering` / `takeover`）
- `spi/degradation.py` 的 8 条降级路径
- "backend 不得自降"原则

Web 前端必须忠实呈现能力位：UI 上能展示"此 backend 当前不支持 streaming，是否降级为整段渲染"，而不是假设所有 backend 都流式输出。

### 3.3 agent-callable Skills + 可验证引用

保留对象：

- `skills/builtin/*/SKILL.md` 的 YAML frontmatter（`name` / `description` 必需）
- `skills/builtin/*/references/source-map.md` 的 SHA256 锚点
- `skills verify` CLI 与 `scripts/regen_source_map.py`
- BackendRunner 在 session start 注入一行 / skill 索引到 system prompt

Web 前端的 Skills 页面**必须**显示每条 skill 的 source-map 引用状态（`verified` / `stale`），并提供"refresh hashes"按钮（在线调用 `scripts/regen_source_map.py` 或对应 Python 函数）。

### 3.4 in-process 多 agent 模式

保留对象：

- `modes/` 下 5 个 `ModeRunner` 实现（`single` / `coordinator` / `pipeline` / `debate` / `swarm`）
- `ModeDecision` dataclass + `ModeSelector` 路由
- `swarm_checkpoint.json` 检查点恢复机制
- debate 模式的独立性约束（proposer 不得读对方输出）

Web 前端的 Sessions 页面需要把"模式"作为 first-class 维度展示，并提供 pipeline / debate 的可视化（见 §5.4）。

### 3.5 一键 install.sh + 后端探测

保留对象：

- `install.sh` 的 6 backend 探测表（`clawcodex`/`claude`/`codex`/`dsh`/`hermes`/`opencode`）+ `auto-detect` 逻辑
- `--backends X,Y` / `--no-backends` / `--all-backends` / `--dry-run` 模式
- `~/.orchestratord/venv` + `activate.sh` 隔离

Web 部署时 install.sh 需要扩展到：

- 新增 `--with-web` flag（启动 FastAPI + 引导 Next.js 静态资源）
- 新增 `--db` 子命令（init / migrate / reset）
- 但**不破坏**现有 CLI 兼容

### 3.6 嵌入式 LiveView 简版

保留对象：

- `cli/dashboard.py` 作为"零依赖开发者模式"继续可用
- 内置事件 feed、SSE、chat UI

Web 上线后这个 LiveView 仍要保留 1 个 release cycle（标记 deprecated），避免强制迁移破坏开发者机器。后续随 Web 稳定逐步下线。

---

## 4. 必须从 multica 借鉴的优势特性

multica 已经实现的、本期 orchestratord 要补齐的产品能力（按优先级）：

| 优先级 | 能力 | 来源（multica） | 实施位置 |
| --- | --- | --- | --- |
| P0 | workspaces + 角色 + 成员管理 | `apps/web/app/[workspaceSlug]/(dashboard)/members` | §6.1 |
| P0 | issues 看板 / 列表 / 详情 / 评论 / 活动 timeline | `(dashboard)/issues` | §5.2.1 |
| P0 | agent 实体（命名 / provider / runtime 绑定） | `(dashboard)/agents` | §6.2 |
| P0 | runtime 机器接入 | `(dashboard)/runtimes` | §6.3 |
| P0 | assignee 多态（member / agent） | `issue.assignee_type` + `assignee_id` | §5.2.1 |
| P0 | execution log 时间线 + 工具调用重放 | `(dashboard)/tasks` 与 `tasks/[id]` | §5.2.3 |
| P1 | token 用量聚合（按 agent / 按 issue / 按 workspace） | `(dashboard)/usage` | §5.2.4 |
| P1 | squads（leader 路由 work 到 members） | `(dashboard)/squads` | §7.1 |
| P1 | projects（关联 repo + docs 的工作集） | `(dashboard)/projects` | §7.2 |
| P1 | skills 浏览（在 board 上展示，复用 §3.3 源） | `(dashboard)/skills` | §5.2.5 |
| P1 | autopilots（cron-like 周期任务） | `(dashboard)/autopilots` | §7.3 |
| P1 | Slack / Lark 通知 + mention 触发 | `apps/web/app/{slack,lark}` | §7.5 |
| P2 | inbox（被 ping 才通知） | `(dashboard)/inbox` | §5.2.6 |
| P2 | 文档站点（Fumadocs，中英双语） | `apps/docs` | §5.6 |
| P2 | GitHub PR 视图 | `server/internal/integrations/github` | §6.5 |
| P3 | GitLab VCS | multica 的 GitLab 适配器 | §6.5 |
| P3 | DingTalk / WeCom / Telegram | multica 对应适配器 | §7.5 |

---

## 5. Web 前端特性缺口（独立章节）

> 这一节是本期投入最重的部分。所有 Web 工作必须遵守下列约束：
> - 不得破坏 §3 任何保留特性
> - 路由、组件、状态层与 multica 心智模型一致，方便跨项目借鉴
> - 后端依赖 FastAPI，**不绕过 `orchestratord` 包**直接调 backend 私有 API

### 5.1 整体定位

**Next.js 16 App Router 单端应用**，目录结构：

```
apps/web/                              # Next.js
  app/                                 # App Router
    (landing)/                         # 未登录 / 营销页
    (auth)/                            # 登录、找回、SSO
    [workspaceSlug]/                   # 工作区 shell（路由级 layout.tsx）
      layout.tsx                       # 工作区守卫（DashboardGuard）
      (dashboard)/                     # 路由组：所有工作区内页面
        issues/                        # 看板 / 列表 / 详情 / 评论
        agents/                        # agent 列表 / 详情 / 新建
        runtimes/                      # 已接入的 runtime 机器
        squads/                        # 团队配置
        projects/                      # 项目工作集
        skills/                        # skill 目录 + source-map 验证状态
        autopilots/                    # 周期任务
        sessions/                      # in-process 模式（pipeline/debate/swarm）实时视图
        inbox/                         # 通知
        members/                       # 成员与角色
        usage/                         # token / 成本聚合
        settings/                      # 工作区设置
  platform/                            # 仅放 Next.js / Router 平台适配
  components/                          # 只放路由级 / 跨页面 UI
packages/
  ui/                                  # 原子组件（Button / Dialog / Tabs...）
  views/                               # 业务视图（按 domain 拆，与 multica 一致）
  core/                                # headless 业务逻辑（API client + React Query + Zustand）
  tsconfig/                            # 共享 tsconfig
  eslint-config/                       # 共享 eslint
```

> 与 multica 的差别：**不输出 `apps/desktop` / `apps/mobile`，不输出 `packages/desktop-views`**。`packages/views/` 只面向 Web，platform/ 仅做 Next.js 适配。

### 5.2 必补页面与路由

#### 5.2.1 Issues（看板 / 列表 / 详情）

源参考：multica `(dashboard)/issues/[id]/page.tsx`

页面组成：

- 列表视图（默认）：状态分组列（queued / pending / running / pending_review / completed / failed / abandoned / verification_failed，复用 orchestratord 现有 9 个 status）+ assignee 多态头像（member 或 agent）+ 最后活动
- 看板视图：拖拽切换 status；右键菜单支持 assign / move / abandon / reopen
- 详情视图（`/issues/[id]`）：
  - 元信息：title / description（markdown）/ status / assignee / labels / linked PR
  - 活动 timeline：`Comment` + `StatusChange` + `RunStart` + `RunEnd` + `ToolCall` + `ApprovalRequest`
  - 评论区（支持 `@agent-name` 触发 mention，见 §7.4）
  - 执行日志 tab（链接到 §5.2.3）
  - 关联 run（pipeline / debate / swarm 各 stage 的子 run）
- 数据契约：复用 `assignee_type` + `assignee_id` 多态

API：

- `GET /api/workspaces/{ws}/issues?status=&assignee_type=&assignee_id=&q=`
- `POST /api/workspaces/{ws}/issues`（创建）
- `PATCH /api/workspaces/{ws}/issues/{id}`（状态 / assignee / labels）
- `POST /api/workspaces/{ws}/issues/{id}/comments`
- `POST /api/workspaces/{ws}/issues/{id}/mention`（`{ agent_id | member_id }`）

#### 5.2.2 Agents

源参考：multica `(dashboard)/agents/{[id],new}/page.tsx`

页面组成：

- 列表：所有 agent 卡片（avatar / name / provider / runtime 引用）
- 详情：能力矩阵可视化（来自 §3.2 的 8 位降级图）、绑定 runtime、关联 skills、最近 runs、token 用量
- 新建（multica "Build with AI" 功能本期不复制——改为表单 + YAML 导入两路）

复用：

- 能力位渲染必须**忠实**反映 `BackendCapabilities`（不得假设所有 backend 都流式 / 都支持 resume）
- skill 列表渲染必须显示 source-map 验证状态

API：

- `GET /api/workspaces/{ws}/agents`
- `POST /api/workspaces/{ws}/agents`
- `GET /api/agents/{id}/capabilities`（直出 `BackendCapabilities` JSON）
- `POST /api/agents/{id}/doctor`（调用现有 `orchestratord backend doctor <name>` 的逻辑）

#### 5.2.3 Sessions（执行日志 / 工具调用重放）

源参考：multica `(dashboard)/tasks` 时间线

页面组成：

- session 列表：按 issue 维度聚合，列出该 issue 下的所有 session + run
- session 详情：
  - 时间轴：每个事件一条（按 `EventEnvelope.kind` 分色：TEXT_DELTA / TOOL_CALL / TOOL_RESULT / APPROVAL_REQUEST / TURN_COMPLETE / PHASE_COMPLETE / SESSION_COMPLETE / ERROR）
  - 重放模式：从某一事件开始，按原始顺序 replay（同会话内可调速 0.5x / 1x / 2x）
  - approval 区：列出 `APPROVAL_REQUEST` 事件并提供 approve / deny 按钮（调用现有 approval policy）
  - 暂停 / 恢复 / 停止按钮
- 多 agent 模式专属视图：
  - **Pipeline**：垂直链路，stage 间箭头 + 上下文注入标注
  - **Debate**：左 / 右 proposer 并列卡片 + judge 卡片；强调"独立思考"标签
  - **Swarm**：动态任务分解图（`task_decomposition.json` 渲染为 wave 树）+ 已完成 / 进行中 / 待办
  - **Coordinator**：任务分发甘特图
  - **Single**：纯时间轴

API：

- `GET /api/sessions/{id}/events?from=&to=`（带 cursor）
- `GET /api/sessions/{id}/events/stream`（SSE，保留为单用户降级；Web 主用 `/ws`）
- `POST /api/sessions/{id}/approve` / `/deny`（复用 `ApprovalPolicy`）
- `POST /api/sessions/{id}/pause` / `/resume` / `/stop`（复用现有 IPC）
- `POST /api/sessions/{id}/messages`（复用 chat gateway）

#### 5.2.4 Usage（token / 成本聚合）

源参考：multica `(dashboard)/usage/page.tsx`

页面组成：

- 概览：今日 / 本周 / 本月 / 自定义区间的 token 与 USD 聚合
- 维度切换：按 workspace / agent / issue / backend
- 图表：折线（每日）+ 表格（每 agent）
- 导出：CSV（保留 CLI 兼容：`orchestratord run logs --export csv`）

复用：

- token 数据从 `EventEnvelope` 中 `SESSION_COMPLETE` 携带的 `usage` 字段汇总（与 README 中 dsh 的 cost_reporting 路径一致）
- USD 字段：clawcodex / claude 报告的 `total_cost_usd`；其他 backend 走 token estimator（与 `degradation.py` 中的 `cost_reporting=False` 路径一致）

API：

- `GET /api/workspaces/{ws}/usage?from=&to=&group_by=`
- `GET /api/agents/{id}/usage`

#### 5.2.5 Skills

源参考：multica `(dashboard)/skills` + orchestratord `src/orchestratord/skills/builtin/`

页面组成：

- 目录视图：每个 skill 一张卡（name / description / source-map verified 状态 / 最后更新）
- 详情：渲染 `SKILL.md` markdown；显示 source-map 引用（每行引用 = 文件路径 : 行范围 : SHA256 前缀）
- 操作：
  - `Refresh hashes`（调用 Python 端 regen）
  - `Verify all`（调用 `orchestratord skills verify`）
- 状态徽标：
  - `verified` — 全部引用通过 SHA256 校验
  - `stale` — 至少一项漂移（显示具体哪一行引用对不上）
  - `missing` — 引用文件已不存在

API：

- `GET /api/skills`
- `GET /api/skills/{name}`
- `GET /api/skills/{name}/source-map`
- `POST /api/skills/{name}/verify`
- `POST /api/skills/refresh-hashes`（受限，仅 admin）

#### 5.2.6 Inbox

源参考：multica `(dashboard)/inbox/page.tsx`

页面组成：

- "被 ping 时"流：APPROVAL_REQUEST / clarification / failure 需要人工介入的事件
- 每条 inbox item：链接到对应 issue / session / event
- 操作：resolve / assign / dismiss

实现：

- 复用 `inbox` 表 + worker（listen `APPROVAL_REQUEST` + `clarification` + `failed` 事件写入 inbox）
- WebSocket 推送新 inbox item

#### 5.2.7 其余页面

| 页面 | multica 参考 | 实现要点 |
| --- | --- | --- |
| Runtimes | `(dashboard)/runtimes` | runtime 接入用两段式：server 给 token，runtime 端 `orchestratord daemon start --workspace-token ...`；Web 上展示 runtime card（机器名 / OS / 已注册 backend 列表 / 心跳） |
| Squads | `(dashboard)/squads` | 见 §7.1 |
| Projects | `(dashboard)/projects` | 见 §7.2 |
| Autopilots | `(dashboard)/autopilots` | 见 §7.3 |
| Members | `(dashboard)/members` | owner / admin / member 角色矩阵；access scopes per member |
| Settings | `(dashboard)/settings` | workspace 设置 / backend 启用 / 通知渠道 / VCS 接入 |
| Landing | `(landing)` | 营销页；登录后路由进工作区 |

### 5.3 设计系统 / 视觉规范

#### 5.3.1 主题与色板

复用 `cli/dashboard.py` 已定义的 CSS 变量（暗色为默认），迁移到 Tailwind 配置：

```
--bg-0  #0b0f17   --accent     #58a6ff
--bg-1  #11161f   --accent-2   #79c0ff
--bg-2  #161c26   --good       #3fb950
--bg-3  #1d2532   --warn       #d29922
--line  #232c3a   --bad        #f85149
                     --purple    #a371f7
                     --vermillion #db6d28
```

亮色主题另出 `:root[data-theme="light"]`，避免色板硬编码。

#### 5.3.2 组件库选型

`shadcn` + Radix Primitives + Tailwind CSS（与 multica `packages/ui` 一致）。

不引入 `@reui` 商业组件库（避免 license 复杂度）。

具体组件优先级：

1. Button / Dialog / Tabs / Card / Tooltip / Dropdown / Toast
2. DataTable（基于 `@tanstack/react-table`，用于 issues / sessions / usage）
3. 看板：自实现 `KanbanBoard`，列 = status，行 = issue
4. 时间线：自实现 `EventTimeline`，复用 multica 的 `execution-log` 心智模型

#### 5.3.3 Token

复用 multica `packages/ui/styles/tokens.css` 的 role-named `--text-*` scale（如 `text-caption` / `text-body` / `text-title`），不引入 Tailwind 默认 `text-sm` / `text-base`。

### 5.4 实时与状态层

#### 5.4.1 协议选型

Web 主用 WebSocket（FastAPI 原生）；SSE 仅作为单机 / 内网降级（避免 WS 代理配置）。

WebSocket 路径：`/ws?workspace_id=&token=`（token 经 query 传，不要进 cookie 因为 WS upgrade 不带 cookie）。

消息形态（参考 multica `server/internal/realtime`）：

```jsonc
// server → client
{ "type": "event", "topic": "issue.{id}", "payload": <EventEnvelope> }
{ "type": "event", "topic": "session.{id}", "payload": <EventEnvelope> }
{ "type": "inbox.created", "payload": {...} }
{ "type": "inbox.resolved", "payload": {...} }
{ "type": "agent.capability.changed", "payload": {...} }

// client → server
{ "type": "subscribe", "topics": ["issue.123", "session.abc"] }
{ "type": "unsubscribe", "topics": ["issue.123"] }
{ "type": "session.approve", "session_id": "...", "tool_call_id": "..." }
```

#### 5.4.2 状态归属

复用 multica 的 "server state via TanStack Query + client state via Zustand" 分层：

- server state：issues / agents / sessions / events / inbox / usage → TanStack Query
- client state：filter / draft / modal / 当前 workspace / tab 布局 → Zustand
- workspace identity：`useWorkspaceId()` 走 React Context；`packages/core` 暴露 `setCurrentWorkspace(slug, uuid)`（仅镜像，不参与路由）
- 不允许 React Context 复制 server 数据
- 不允许把 server payload 镜像到 Zustand

#### 5.4.3 WebSocket ↔ TanStack Query 桥接

复用 multica 心智模型：

- WS 消息触达 → 调用 `queryClient.invalidateQueries(...)` 或 `queryClient.setQueryData(...)`
- 不在 WS handler 内手写缓存合并，避免与 React Query 的 stale-while-revalidate 冲突
- 乐观更新：仅用于"结果可预测 + 不跳转 + 失败罕见 + 回滚简单"的场景（assignee / status / label 切换）

### 5.5 工程化（前后端拆分 / API / 构建）

#### 5.5.1 后端 API 拆分

现状：`cli/dashboard.py` 一个 `BaseHTTPRequestHandler` 包揽所有路由（2306 行）。Web 上线前必须拆分：

- 引入 FastAPI app（`apps/api/main.py`），保留 Typer CLI 作为 compat
- 路由按 domain 拆 router：`routers/issues.py` / `routers/agents.py` / `routers/sessions.py` / `routers/skills.py` / `routers/usage.py` / `routers/inbox.py` / `routers/runtimes.py` / `routers/squads.py` / `routers/projects.py` / `routers/autopilots.py` / `routers/channels.py`
- `/dashboard` 旧路由继续以 compat shim 存在，1 release cycle 后 deprecated
- OpenAPI 自动生成（FastAPI 原生），落地到 `/docs`（开发态）

#### 5.5.2 Web 构建

- Next.js 16 App Router（不用 Pages Router）
- Turborepo 编排（同 multica），新增 `apps/api` 与 `apps/web`
- 包管理：与 multica 一致用 `pnpm`（与现有 Python `uv` 不冲突）

#### 5.5.3 测试

按 multica 测试分层：

| 层 | 工具 | 范围 |
| --- | --- | --- |
| Python 单元 | pytest | orchestrator / SPI / skills / modes / backend_registry |
| Python 集成 | pytest + httpx | FastAPI router（接真实 DB） |
| TS 业务逻辑 | Vitest（`node` 环境） | `packages/core` 纯逻辑、Zustand store、API client 解析 |
| TS UI | Vitest + Testing Library | `packages/views` 业务组件 |
| 平台 wiring | Vitest（`jsdom` 环境） | `apps/web` 仅做 Next.js 路由 / cookies / search params |
| E2E | Playwright | 关键路径（创建 issue → assign agent → 实时看 session → 评论） |

复用 multica 的真 agent smoke 测试规范：

- 默认测试**绝不** resolve / execute 用户安装的 agent CLI
- 真实 agent 烟雾测试必须放 `agentintegration` build tag 后，**仅**当 `ORCHESTRATORD_RUN_REAL_AGENT_SMOKE=1`
- 新增 default agent 命令必须写入 `scripts/agent-cli-command-names.txt`

### 5.6 i18n 与文案规范

复用 multica 的 i18n 基础设施：

- 文案 source of truth：`apps/docs/content/docs/developers/conventions.mdx`（含中英文术语对照表）
- 翻译文件位置：`packages/views/locales/{en,zh-CN}/...`
- 中文产品文案规范：动词优先（"添加"、"分配"、"触发"），不用"进行 XX 操作"
- 路由命名规范：单段（`/login`、`/inbox`）或 `/{noun}/{verb}`（`/workspaces/new`）；禁止 `/new-workspace` 这种连字符根路由
- Reserved slugs：`server/internal/handler/reserved_slugs.json`，编辑后 `pnpm generate:reserved-slugs` 重新生成 `packages/core/paths/reserved-slugs.ts`

### 5.7 安全 / 权限 / 多工作区

#### 5.7.1 角色矩阵

| 角色 | 可做 |
| --- | --- |
| owner | 所有 + 删除工作区 + 转移所有权 |
| admin | 成员管理 / agent 管理 / VCS 接入 / 通知渠道配置 |
| member | 创建 issue / 评论 / 触发 autopilots / 看自己的 usage |

multica 的 access scopes（per member 能跑哪些 agent）按 multica 实现：members 表 + `member_agent_scopes` 多对多表。

#### 5.7.2 多工作区切换

路由：`/{workspaceSlug}/...`，layout.tsx 中 `DashboardGuard` 校验成员资格，未通过跳 `/login` 或 `/workspaces/new`。

跨工作区导航必须走 `useNavigation().push()` 或 `<AppLink>`（同 multica），不直接 `<a href>`。

`setCurrentWorkspace(slug, uuid)` 由路由 layout 触发，不要在组件内手动调用。

#### 5.7.3 审计

每条 Web 触发的 mutation（创建 issue / 重派 / approve）必须写 `audit_log` 表：

- `id` / `workspace_id` / `actor_type`（member / agent / system）/ `actor_id` / `action` / `target_type` / `target_id` / `payload_jsonb` / `created_at`
- Web 端的"管理员审计"页面提供筛选与导出

#### 5.7.4 凭证与认证

- Web 端 cookie-based session（httpOnly + Secure + SameSite=Lax）
- daemon 端 runtime token：long-lived bearer，用于 WS 连接与 daemon → server 的反向心跳
- API token：multica 风格的 `auth_tokens` 表（name / token_hash / scopes / expires_at）
- 用户安装的 agent CLI 默认**不可**直连 server API；只能由 daemon 中转

---

## 6. 后端 / 数据层缺口（支撑 Web 所必需）

### 6.1 PostgreSQL 引入

数据库版本：PostgreSQL 17，扩展 `pgcrypto` + `pg_trgm`。

**Schema 迁移规则**（沿用 multica `CLAUDE.md`）：

- 不加 FK / cascading delete / cascading update（用应用层解决）
- 每个索引必须 `CREATE INDEX CONCURRENTLY` 或 `CREATE UNIQUE INDEX CONCURRENTLY`
- 每次 `CREATE INDEX CONCURRENTLY` 单独一个迁移文件（不能放进事务）
- runner 在迁移文件外执行以支持非事务场景
- 条件性跳过的迁移也记入 `schema_migrations`，后续引用条件对象的迁移必须用 `IF EXISTS` / `IF NOT EXISTS`

#### 6.1.1 起步 schema

```
workspaces
members
member_agent_scopes
agents
agent_capabilities_cache    -- 缓存 BackendCapabilities，backend 启动时刷新
runtimes                    -- 已接入的机器
runtime_backends            -- runtime 上探测到的 CLI 列表
issues
issue_comments
issue_labels
issue_status_history
sessions                    -- 一次会话 = 一次 BackendRunner.run
runs                        -- 一次 run = workflow 的一次执行
events                      -- EventEnvelope 持久化（按时间序 + run_id + session_id 索引）
skills
skill_source_maps
skill_references            -- 每条 source-map 的 (file, line_range, sha_prefix)
approvals
inbox
usage_aggregates             -- 按 (workspace_id, agent_id, day) 聚合
squads
squad_members
projects
project_repos
project_docs
autopilots
autopilot_runs
audit_log
auth_tokens
```

#### 6.1.2 事件持久化策略

- `events` 表水平拆分：按 `created_at` 月分区（partition by range）
- 单条事件 payload：`jsonb`，索引用 `gin (payload jsonb_path_ops)` 仅用于排查
- 典型查询索引：`(session_id, sequence)` 升序、`(workspace_id, created_at desc)`、`(issue_id, created_at desc)`
- 不在 DB 层做重活：聚合查询走 materialized view，按小时刷新

### 6.2 Agent 实体化

multica 的 agent 是 DB 实体（`agents` 表）+ 运行时 backend 解耦。orchestratord 当前 backend 是 entry point 直接返回 `AgentBackend`，没有"实例"概念。

需要新增的概念：

- `Agent`（持久化）：`id` / `workspace_id` / `name` / `provider`（= backend 名）/ `runtime_id` / `capabilities_cache_jsonb` / `created_at`
- 启动时（daemon 启动 / agent 新建 / backend 重连）：调用 `BackendRunner.describe()` → 写入 `agent_capabilities_cache`
- Web 前端读 `capabilities_cache_jsonb` 渲染能力矩阵

### 6.3 Runtime 机器接入

- server 生成一次性 token（`runtime_id` + `token`）
- runtime 端执行 `orchestratord daemon start --workspace-token $TOKEN --workspace-id $WS`
- runtime 周期性心跳（30s）→ server 端更新 `runtime.last_seen_at`
- runtime 探测本机 CLI 列表 → 上报 `runtime_backends`（哪些 CLI 装在哪些 runtime 上）
- Web 上"runtimes"页显示每个 runtime 的 backend 覆盖

### 6.4 WebSocket 服务

- 复用 `server/internal/realtime` 形态：topic-based pub/sub
- 单实例：进程内 asyncio.Queue；多实例：Redis pub/sub 中继
- daemon → server 上行心跳与事件走同一 WS
- 心跳间隔 30s（与 multica `HeartbeatInterval` 默认一致）

### 6.5 VCS 集成

本期 GitHub only；GitLab 后置。

复用 multica `server/internal/integrations/github` 的契约：

- `installations` 表：每工作区一份 GitHub App 安装
- `pull_requests` 表：每个 issue 关联的 PR 列表
- webhook handler：处理 `issues` / `pull_request` / `check_run` / `push` 事件
- Web 上 PR 视图：在 issue 详情页内嵌 PR 状态 / checks / review

issue→PR 应用（`applications/issue_pr.py`）保持现有形态，作为 `pull_requests` 表的写入入口；不重写。

---

## 7. 协作 / 产品抽象缺口

### 7.1 Squads

数据模型：

```
squads           id / workspace_id / name / leader_type / leader_id / created_at
squad_members    squad_id / member_type / member_id
```

行为：

- leader（member 或 agent）路由 issue 到 members
- leader 可以是 agent（基于其能力：goal_mode=True 的 backend）
- Web 上 squad 详情页：成员列表 + 最近被 leader 路由的 issue

API：

- `POST /api/squads`
- `POST /api/squads/{id}/assign`（leader 主动分配）
- `POST /api/squads/{id}/route`（自动路由请求）

### 7.2 Projects

数据模型：

```
projects         id / workspace_id / name / description
project_repos    project_id / repo_url / default_branch
project_docs     project_id / doc_url / doc_type (md|html|pdf)
```

行为：

- 创建 issue 时可选关联 project
- 关联后 agent 在 session start 时自动注入 project 内的 repo 路径 + docs 摘要（multica 的 "attach the repos and docs agents need as context"）

### 7.3 Autopilots

数据模型：

```
autopilots       id / workspace_id / name / cron / prompt / target_kind / target_id / enabled
autopilot_runs   autopilot_id / scheduled_at / started_at / finished_at / status / run_id
```

实现：

- 复用 `apscheduler` 或 asyncio task loop（轻量）
- schedule → 启动对应 workflow（autopilot 即一个声明式 workflow）
- Web 上 autopilots 列表 + run 历史

### 7.4 Mention / Chat

mention 解析：

- 评论文本扫描 `@agent-name` 与 `@member-name`
- 触发：派发 inbox item + 写 mention event
- agent 收到 mention 触发新 session（复用 `intent.py` 与 `mode_selector.py`）

chat（multica 的 chat 模式）：workspace-level chat，不建 issue 也能触发 session。本期做最小版本（一次 prompt 一次 session），不引入 MCP。

### 7.5 通知渠道（Slack / Lark）

复用 multica 的 `apps/web/app/{slack,lark}` 设计：

- 每工作区一套 OAuth 接入
- `channels` 表：每工作区 N 个 channel 绑定
- channel 触发：在 channel 内 `@orchestratord <issue-id 或自然语言>` → 创建 / 派发 issue
- channel 推送：session 状态变化（running → pending_review）→ 推送到对应 channel
- mention 同 §7.4

DingTalk / WeCom / Telegram 后置：

- adapter interface 预留（multica 的 `apps/web/app/{dingtalk,wecom,telegram}` 是参考）

---

## 8. Agent 后端覆盖缺口

### 8.1 缺口矩阵

multica 注册的 26 个 CLI 中，orchestratord 当前缺 20 个。按优先级 + 实施难度排序：

| # | backend | 优先级 | 难度 | 协议 | 备注 |
| --- | --- | --- | --- | --- | --- |
| 1 | `cursor` | P1 | 中 | CLI | spawn `cursor-agent`，JSON 输出解析 |
| 2 | `copilot` | P1 | 中 | CLI | `copilot` CLI；事件流需实验 |
| 3 | `kimi` | P1 | 中 | CLI | `kimi`；中文 prompt 友好 |
| 4 | `qwen` | P1 | 中 | CLI（stream-json） | multica：`qwen -p --output-format stream-json` |
| 5 | `grok` | P2 | 中 | ACP | `grok agent --always-approve stdio` |
| 6 | `kiro-cli` | P2 | 中 | CLI | |
| 7 | `openclaw` | P2 | 中 | HTTP | `openclaw agent --local ...` vs Gateway 路由 |
| 8 | `agy` (antigravity) | P3 | 高 | 私有 | |
| 9 | `codebuddy` | P3 | 高 | ACP | |
| 10 | `qodercli` / `qoderclicn` | P3 | 高 | ACP | |
| 11 | `deveco` | P3 | 高 | ACP | |
| 12 | `codearts` | P3 | 高 | 私有 | |
| 13 | `pi` / `omp` (oh-my-pi) | P3 | 中 | Pi 协议 JSON | |
| 14 | `qwenpaw` | P3 | 中 | ACP | per-task workspace |
| 15 | `reasonix` | P3 | 中 | CLI | |
| 16 | `mcode` | P3 | 高 | 私有 | |
| 17 | `dim` | P3 | 高 | 私有 | |
| 18 | `traecli` | P3 | 中 | 私有 | |
| 19 | `zeroclaw` | P3 | 中 | CLI | |

### 8.2 实施模板（每个新 backend 的标准流程）

1. 新建 `backends/orchestratord-<name>/` 包
2. pyproject.toml：`[project.entry-points."orchestratord.backends"]` + `"orchestratord.backend_descriptors"`
3. `backend.py` 实现 `AgentBackend` Protocol + 能力位如实报告
4. `session.py` 翻译 backend 原生事件到 `EventEnvelope`
5. `descriptor.py` 实现 `BackendDescriptor`（family / score / notes）
6. `tests/`：复用 `tests/contracts/` 的 T1–T9 contract tests + 防真实 CLI 调用守门
7. 更新 `tests/test_capability_drift.py`：新增 backend 必须通过 CI 守门
8. `install.sh`：加入新 backend 的 runtime 探测表
9. `_backend_cli_registry`：CLI 名加入
10. `scripts/agent-cli-command-names.txt`：加入 daemon 端默认命令清单
11. README 能力矩阵 + 事件流表更新

### 8.3 ACP 适配器

为降低 §8.1 中 7 个 ACP backend 的实现成本，本期一次性抽出 `orchestratord-acp` 包：

- 实现通用 ACP backend（基于 `@agentclientprotocol/sdk` Python 版或自写 JSON-RPC stdio）
- 新增 ACP backend 时只填 descriptor + per-id 默认值（如 qoderclicn 的 `MULTICA_QODERCLICN_PATH` 风格）
- 优先支持 `session/request_permission` + `session/cancel`（与 multica 一致）

### 8.4 multica 风格 runtime 身份与协议家族分离

借鉴 multica 的 `BuiltinRuntime` 模式：

- `protocol_family` 与 `runtime_id` 分离
- 一个 protocol family 可承载多个 runtime id（如 multica 的 `omp` → `pi`）
- orchestratord 的实现路径：在 `spi/backend_descriptor.py` 增加 `protocol_family` 字段，`backend_registry._classify_family()` 同时支持 id 与 family 两层

---

## 9. 实施路线图（Phase 0–5）

### Phase 0 — 基础兼容（2 周）

- 拆 `cli/dashboard.py` 为 FastAPI app（保留 compat shim）
- 引入 PostgreSQL（最小 schema：workspaces / members / agents / sessions / runs / events）
- 迁移事件日志为 DB 持久化（保留磁盘 JSONL 作为审计 fallback）
- 保留 `install.sh` + `cli/dashboard.py` LiveView 不动

验收：`orchestratord dashboard --port 8080` 仍可用；新增 `orchestratord serve --port 9000` 启动 FastAPI。

### Phase 1 — Web 雏形（3 周）

- Next.js 16 项目脚手架（apps/web + packages/core + packages/ui + packages/views）
- 主题色板迁移（来自 §5.3.1）
- 路由骨架：登录 / 工作区 layout / issues 列表（只读）
- FastAPI router：issues / agents / sessions
- WebSocket 接入 + TanStack Query 桥接

验收：能在浏览器看到 issues 列表与详情；实时事件能从 daemon 推过来。

### Phase 2 — 核心交互（4 周）

- 看板视图 + drag-drop status 切换
- 评论 + mention
- session 时间线 + 重放
- 能力矩阵渲染（§5.2.2）
- skills 页面（§5.2.5）+ source-map verify

验收：能在浏览器跑通 "创建 issue → assign agent → 看 session 时间线 → 评论 → 触发 autopilots"。

### Phase 3 — 协作层（3 周）

- Squads / Projects / Autopilots 实体 + 页面 + API
- Runtimes 接入 + 两段式 token
- Inbox
- Usage 聚合视图

验收：能组成 5 个 agent 的 squad，autopilots 周期触发并产出 issue。

### Phase 4 — 后端覆盖扩展（持续）

- 按 §8.1 优先级补 backend
- 抽出 `orchestratord-acp` 通用包
- 引入 `protocol_family` / `runtime_id` 分离

验收：backends 总数从 6 增至 ≥ 12；ACP 通用包至少覆盖 3 个 ACP backend。

### Phase 5 — 通知 + 多 VCS（2 周）

- Slack / Lark 接入
- GitHub VCS 集成（issues / PR / webhook）
- 文档站点（Fumadocs，中英双语）

验收：Slack 内 `@orchestratord` 创建 issue；issue 详情页内嵌 GitHub PR 状态；文档站点可访问。

---

## 10. 兼容性 / 迁移策略

### 10.1 CLI 兼容

- 保留所有现有 `orchestratord` 子命令语义
- `dashboard` 子命令保留 ≥ 1 个 release cycle
- `run` / `workflow` / `skills` / `backend` / `app` 子命令不变
- 新增 `serve` / `web` 子命令（不替换 `dashboard`）

### 10.2 数据库迁移

- 旧磁盘事件日志：一次性脚本 `scripts/migrate_eventlog_to_db.py` 导入
- 单工作区用户：自动迁移到 default workspace
- 多工作区：手工创建 + 导入

### 10.3 后端包兼容

- 现有 6 个 backend 包（`orchestratord-clawcodex` 等）**保持 ABI 兼容**
- 新增 `agent_capabilities_cache` 字段对老 backend 无影响（启动时探测失败也能启动）
- 测试守门：`tests/test_capability_drift.py` 任何 backend 改动必须通过 CI

### 10.4 Skills 兼容

- `SKILL.md` + `references/source-map.md` 格式不变
- `skills verify` CLI 保留
- Web 上 `/api/skills/{name}/source-map` 仅做只读视图

### 10.5 部署兼容

- `install.sh` 加 `--with-web` / `--with-db` flag
- 默认 `--no-web --no-db`（开发者模式，与现状一致）
- `--all` = 全部启用

---

## 11. 技术栈选型建议

### 11.1 后端

| 选择 | 理由 |
| --- | --- |
| FastAPI | 与现有 Python 一致；原生 OpenAPI + WebSocket + SSE；类型注解与 Pydantic 已用 |
| SQLAlchemy 2.x | 异步支持成熟；与现有 `pydantic` 数据契约对接 |
| Alembic | 与 FastAPI 集成；支持 non-transactional migrations（multica 风格） |
| asyncpg | 高性能 PostgreSQL 驱动 |
| Redis | 多实例部署时 WS 中继；单实例可省 |
| APScheduler | autopilot cron（与 Python 异步集成） |

### 11.2 前端

| 选择 | 理由 |
| --- | --- |
| Next.js 16 App Router | 与 multica 一致；server components 减轻 Web 渲染压力 |
| TanStack Query | server state 唯一来源 |
| Zustand | client state；与 multica 一致 |
| shadcn + Radix | 不引入商业 license |
| @tanstack/react-table | DataTable 基础 |
| Tailwind CSS | 与 multica 一致；CSS 变量语义化 |
| react-flow | session 时间线 / pipeline / debate / swarm 可视化 |

### 11.3 工程化

| 选择 | 理由 |
| --- | --- |
| pnpm | 与 multica 一致；与 Python `uv` 不冲突 |
| Turborepo | 与 multica 一致 |
| Playwright | E2E；与 multica 一致 |
| pytest | Python 单元 / 集成 |
| Vitest | TS 单元 / 组件 |
| ruff | Python lint（已有） |
| ESLint + Prettier | TS lint |
| GitHub Actions | CI；与 multica 一致 |

---

## 12. 风险与权衡

### 12.1 架构层面

| 风险 | 缓解 |
| --- | --- |
| 多租户引入复杂度破坏单进程简洁 | 默认单工作区模式可用；多租户是 opt-in |
| 数据库 schema 演进失控 | 严守 multica 迁移规则（concurrent index / 无 FK / 单文件迁移） |
| WebSocket 反向心跳与现有 control socket 重复 | control socket 是 daemon 本机；WS 是 daemon → server，作用不同 |
| 嵌入式 LiveView 与 Web 长期共存造成双前端维护成本 | 1 release cycle 后 dashboard 子命令 deprecated；之后彻底移除 |

### 12.2 产品层面

| 风险 | 缓解 |
| --- | --- |
| squads / projects / autopilots 是 multica 强项但 orchestratord 缺工程积淀 | Phase 3 拆分独立子项目；先做最小可用版本 |
| 补 backend 数量时 backend 作者质量参差 | 严守 contract tests + 守门测试；CI 强制 `BackendCapabilities` 报告完整 |
| Slack / Lark 通知反向要求 Web 路由可达 | 自托管默认端口固定（9000 / 3000）；提供 webhook 内网穿透指引 |

### 12.3 安全层面

| 风险 | 缓解 |
| --- | --- |
| daemon token 泄露 | token 单向 hash 存储；rotate 流程 + 审计日志 |
| 用户安装的 agent CLI 被 Web 直连 | 物理隔离：agent CLI 仅 daemon 中转；server 不接受 agent CLI 直连 |
| 多工作区数据越权 | 严守 `X-Workspace-ID` 头 + `workspace_id` 过滤（multica 规则） |
| Skills source-map 漂移被忽略 | `skills verify` 在 daemon 启动时强制执行；CI 守门；Web 端显示 verified 状态 |

### 12.4 工程纪律

| 风险 | 缓解 |
| --- | --- |
| 默认测试真实调用 agent CLI | 复用 multica 的 `agentintegration` build tag + 默认不可 resolve |
| backend 数量增加后 install.sh 探测表膨胀 | 探测逻辑统一（`detect_runtime` helper）；6 → 26 探测代码量线性增长 |
| 与 clawcodex 的 strangler-fig 迁移未完成 | 严守 §3.1；新 Web 不绕过 `orchestratord` 直接调 backend 私有 API |

---

## 13. 验收标准

### 13.1 功能验收（每个 Phase 完成后）

| Phase | 验收产物 |
| --- | --- |
| Phase 0 | `orchestratord serve` 启动；旧 `dashboard` 仍可用；DB schema 初始化脚本通过 |
| Phase 1 | 浏览器能看到 issues 列表与详情；WS 实时推送 1 个 backend 的真实事件 |
| Phase 2 | 端到端走通 "创建 issue → assign agent → 看 session → 评论 → 触发 autopilots" |
| Phase 3 | squad leader 路由 work；autopilots cron 触发；inbox 通知到位；usage 聚合准确 |
| Phase 4 | backend 数 ≥ 12；ACP 通用包至少覆盖 3 个 backend |
| Phase 5 | Slack 创建 issue；GitHub PR 视图嵌入；文档站点可访问；i18n 中英双语完整 |

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

- 多工作区数据隔离：跨工作区访问被拒
- audit_log 覆盖所有 mutation
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
| Backend packages | `backends/orchestratord-{clawcodex,claude,codex,dsh,hermes,opencode}/` |
| Install | `install.sh` |

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

### 14.3 文档演进

- v1（当前）：草案，Phase 0–5 路线图
- v2：Phase 0 完成时补充实际 schema 与 FastAPI router 列表
- v3：Phase 2 完成时补充 WS 协议样例
- v4：Phase 5 完成时总结"Web 上线后 multica vs orchestratord 对比"

---

**变更记录**

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-09-04 | 初稿；范围 §1.1–§1.2 划定；Phase 0–5 路线图 |
