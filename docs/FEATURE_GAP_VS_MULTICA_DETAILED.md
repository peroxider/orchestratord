# orchestratord vs multica — 详细特性缺口分析

> 状态：实测对比 v2（已同步 v2 主文档）
> 范围：仅对比 **可视化平台** 与 **Agent 后端支持** 两个维度
> 数据来源：当前 working tree（2026-09-07）的目录与代码事实
> 配套关系：本文档是 `docs/FEATURE_GAP_VS_MULTICA.md` v2 的**量化基线 + 逐视图 / 逐 family 拆解附录**；v2 是规划主体（Phase A–E），本文档提供实测数据供规划核对
> 重要：v1 → v2 后单用户模式（D9）+ 数据层保留（D10）+ Phase A 优先级（D11）已生效；本文档 §4 的"Phase A/B/C"标签按 v1 视角保留，需按 v2 §5/§6/§7/§8/§9 对照阅读

---

## 0. TL;DR

- **可视化平台**：multica 是产品级 "AI 任务管理平台"（Next.js + Electron 桌面 + Expo 移动 + Fumadocs 文档站）。orchestratord v2 周期开始时**骨架已落地但实现深度仅是路由级 stub** —— FastAPI / PostgreSQL / WebSocket / Next.js 骨架与 18 个 router 已在位，`apps/web` 14 个 dashboard 路由 + `packages/views/{agents,audit,autopilots,inbox,issues,members,projects,runtimes,sessions,skills,squads,usage,vcs}` 14 个域 stub 摆好（单页 11~61 行），组件实现在 `packages/views` 也只有 35 个文件（multica 同口径 1050 个文件）。v2 §2 实测基线对齐本文档实测快照。
- **Agent 后端**：orchestratord 真正"硬实现"的 backend 是 15 个（其中 5 个只是把 capability matrix 报满的薄壳），multica `SupportedTypes` 列了 26 个 protocol family + 1 个 builtin runtime 派生（`omp` → `pi` family）；multica 每个 agent 都有独立文件 + ACP/stream-json/app-server 三套抽象，orchestratord 仅在 clawcodex/codex/dsh/opencode 上有像样的事件翻译。
- **核心结论**：缺口不在"协议位能否补齐"（SPI 与 capability matrix 已就位），而在 (a) 可视化的深度产品组件、(b) ACP 通用适配器、(c) 多客户端形态（缺 Desktop 与 Mobile）。

---

## 1. 可视化平台缺口

### 1.1 客户端形态：multica 三个端，orchestratord 只有一个

| 端 | multica | orchestratord | 备注 |
| --- | --- | --- | --- |
| Web | `apps/web`（Next.js 16 App Router，14 个 dashboard 子路由） | `apps/web`（Next.js，14 个 stub 页面） | 路由数对齐，但实现差距极大 |
| Desktop | `apps/desktop`（Electron + electron-builder + 自定义 vite + DragStrip/WindowOverlay） | ❌ 缺失 | 草案明确 D5 不做 |
| Mobile | `apps/mobile`（Expo / React Native，有独立 `apps/mobile/CLAUDE.md`） | ❌ 缺失 | 草案明确 D5 不做 |
| Docs | `apps/docs`（Fumadocs + i18n） | `apps/docs`（骨架，仅有 `app/content/lib/proxy.ts`） | 草案目标已基本对齐 |

multica 共享层 `(packages/views)` 同时为 web + desktop 提供代码 —— `packages/views` 中 `chat/`、`common/task-transcript/`、`runtimes/components/charts/` 等在两端直接复用，desktop 仅多一层 `apps/desktop/src/renderer/src/platform/`（react-router 适配）。orchestratord 走的是"只做 Web"路线，所以 `packages/views` 35 个文件全部面向 Web。

### 1.2 路由骨架：文件名对齐，深度不在同一量级

```
orchestratord/apps/web/app/[workspaceSlug]/(dashboard)/    14 个页面文件，最长 61 行
multica/apps/web/app/[workspaceSlug]/(dashboard)/          28 个 .ts/.tsx 文件，含子目录 issues/[id]/
```

orchestratord 已经把 14 个 dashboard 路由**全部占位**（agents/audit/autopilots/inbox/issues/issues[id]/members/projects/runtimes/sessions[id]/skills/skills[name]/squads/usage）。但是：

- `apps/web/app/page.tsx`（11 行）—— 仅一个 `<h1>orchestratord</h1>` + `<Link href="/login">`，没有营销页、没有 onboarding、没有工作区切换器。
- `apps/web/app/[workspaceSlug]/(dashboard)/issues/page.tsx`（11 行）—— 只 import 了 `IssuesBoard` 并塞 slug，没有 router-segment 级别 Suspense/loading/error 边界。
- `apps/web/app/[workspaceSlug]/(dashboard)/agents/page.tsx`（11 行）—— 直接渲染 `@orchestratord/views` 的 `AgentsList`，没接 DashboardGuard、没有数据预取、没有 i18n provider 包裹。
- 整套 web 的 `layout.tsx` 17 行，**没有 root provider tree**（无 I18nProvider、无 QueryClientProvider、无 ThemeProvider、无 AuthGuard）。

multica 的对应：

- `apps/web/app/[workspaceSlug]/(dashboard)/layout.tsx` 至少挂 `DashboardLayout` + `SearchCommand` + `FloatingChat` + `WebNotificationBridge` + `WorkspaceDocumentTitle`，并用 `Suspense` 隔离 `useSearchParams`。
- `apps/web/app/web-providers.tsx`、`theme-provider.tsx`、`web-notification-bridge.tsx` 三件套是 web 平台 wiring。

### 1.3 视图深度：multica 1050 个文件 vs orchestratord 35 个文件

直接对比：

| 域 | multica (`packages/views/<domain>`) | orchestratord (`packages/views/src/<domain>`) |
| --- | --- | --- |
| agents | `agents/` + `agents/components/` + `agents/create/ai-builder-session-page.tsx` + `use-builder-session.ts` | 单文件 `agents-list.tsx` + `capability-matrix.tsx` + `capability-labels.ts` |
| issues | `issues/{actions,components,hooks,surface,utils}/` —— `actions/` 含 `use-issue-actions.ts` + `run-confirm-gate.ts`；`components/` 含 board/board-column/board-card、batch-action-toolbar、comment-card、comment-input、comment-trigger-chips、execution-log-section、gantt-view、data-table-resize、custom-status-chip 等 | 单文件 `issues-list.tsx` + `issue-detail.tsx` + `kanban-board.tsx` + `status.ts` |
| chat | `chat/` + `chat/components/` + `chat/lib/`（含 `chat-session-header.tsx`、`session-rename-input.tsx`、`task-status-pill.tsx`、`use-chat-task-actions.ts`） | ❌ 无 |
| sessions / 执行日志 | `common/task-transcript/`：`agent-transcript-dialog.tsx`、`build-timeline.ts`、`run-timeline.tsx`、`transcript-button.tsx`、`transcript-follow.ts` | 单文件 `event-timeline.tsx` + `session-detail.tsx` + `event-kind.ts` |
| inbox | `inbox/` + `inbox/components/` | 单文件 `inbox-list.tsx` + `inbox-status.ts` |
| runtimes | `runtimes/` + `runtimes/components/charts/{daily,weekly}-tasks-chart.tsx` | 单文件 `runtimes-list.tsx` + `runtime-status.ts` |
| skills | `skills/` + `skills/components/` + `skills/hooks/` + `skills/lib/` | 单文件 `skills-list.tsx` + `skill-detail.tsx` |
| members / squads / projects / autopilots / usage / audit | 每个域均有 `components/` 子目录 | 单文件 stub |
| rich-content / editor | `rich-content/` + `editor/{extensions,hooks,styles,utils}/`（含 Tiptap/Base UI 富文本） | ❌ 无 |
| attachments / labels / billing | `attachments/`、`labels/`、`billing/` 三个独立域 | ❌ 无 |
| onboarding | `onboarding/` + `onboarding/components/` + `onboarding/steps/` + `onboarding/templates/` | ❌ 无 |
| invitations / invite | `invitations/`、`invite/` | ❌ 无 |
| settings（含 i18n 切换 / VCS 配置 / 通知配置） | `settings/` + `settings/components/` | ❌ 无 |
| 通知渠道（5 个） | `slack/`、`lark/`、`dingtalk/`、`telegram/`、`wecom/` 各自一个域 | ❌ 无 |

`packages/views/locales/`：multica 有 `en/ja/ko/zh-Hans/` 四语，orchestratord `i18n/locales/{en,zh-CN}.ts`（两份字典文件，不到一页长度）。

### 1.4 可视化能力（multica 独有 / orchestratord 缺失）

- **Gantt 视图**：`packages/views/issues/components/gantt-view.tsx` —— orchestratord 完全没有"时间维度排程"的页面。
- **批量操作工具栏**：`batch-action-toolbar.tsx` + `.test.tsx` + `.confirm.test.tsx` —— orchestratord 单 issue 操作还没做完。
- **执行日志 section（嵌入 issue 详情页）**：`execution-log-section.tsx` —— orchestratord 把 session 拆到了独立 `/sessions/[id]` 路由，没有"嵌回 issue 详情"的复用。
- **Task transcript 浮层对话框**（agent 完整 transcript 弹窗）：`common/task-transcript/agent-transcript-dialog.tsx` + `build-timeline.ts` + `run-timeline.tsx` + `diff-highlight.ts` —— orchestratord 仅有扁平 `event-timeline.tsx`，没有 diff 着色、没有 transcript 对话框、没有 transcript-follow 跟踪机制。
- **Assignment picker / board-card-assignee-picker**：multica 多态 assignee（member / agent）单选组件。
- **Comment-trigger-chips**：评论触发器（自动 mention、引用 run、引用 issue）。
- **Tiptap 编辑器 + 富文本扩展**：issues 描述支持完整富文本。
- **Onboarding 步骤编排**：templates + steps + components —— orchestratord 没有"第一次进入工作区"的引导。
- **Mention / chat 触发**：`chat/` 完整模块 + `@orchestratord` mention；orchestratord 仅在 issues 评论区有 i18n 占位文本，没有 chat 域、没有 mention 解析器。
- **实时浮窗 Search Command + Floating Chat**：`views/search`、`chat/floating-chat.tsx` —— orchestratord 没有 command palette。
- **Inbox 子组件**：multica `inbox/components/` 下含 APPROVAL_REQUEST / failure / clarification 的差异化视图；orchestratord 仅一个列表。
- **Runtimes 图表**：`runtimes/components/charts/{daily,weekly}-tasks-chart.tsx` —— orchestratord runtimes 仅一张卡片列表。
- **审计页**：multica 把 audit 落在 `apps/web` 而非 views（说明是 web 专属页面）；orchestratord `packages/views/src/audit/` 单文件 stub。

### 1.5 实时通道

- orchestratord：CLI `cli/dashboard.py`（1464 行，19 个类/函数）的 `BaseHTTPRequestHandler` SSE + 单进程；FastAPI `/ws` 路由已落地但仅骨架（heartbeat + subscribe/unsubscribe + ack，`_ws_token_valid()` 只拒空值/`bogus` 哨兵），topic pub/sub backbone 未接（详见主文档 v2 §5.1）。
- multica：独立 `server/internal/realtime/`（gorilla/websocket，topic-based pub/sub），daemon → server 反向心跳 30s。

### 1.6 多租户 / 数据库

- orchestratord：`alembic/` 起步；草案计划 Phase 0 引入 PostgreSQL 17 与 workspaces / members / agents / sessions / runs / events 等 26 张表。
- multica：已经是 `server/internal/migrations/` 120+ 个 migration（含 `protocol_family CHECK` 等） + sqlc 生成 + PostgreSQL + pg_trgm/pgcrypto 扩展。

---

## 2. Agent 后端缺口

### 2.1 总数与深度

| 指标 | orchestratord | multica |
| --- | --- | --- |
| `SupportedTypes` / entry-points 数量 | 15 backend 包（其中 clawcodex/claude/codex/dsh/opencode 是"硬实现"，其余 10 个薄壳/stub） | 26 个 protocol family + 1 个 builtin runtime（`omp` → `pi`） |
| 真正的协议翻译 | clawcodex (1653 行)、dsh (1440 行)、codex (987 行)、opencode (594 行)、claude (631 行) —— 这 5 个有真 session 翻译 | 每个 family 一个 `.go` 文件 + `*_test.go`（qoder 999 行、pi 633 行、antigravity/codearts/codebuddy/copilot/cursor/reasonix/qwen/qwenpaw/traecli/zeroclaw 等），78k 行单目录 |
| Stub / 仅有 descriptor | copilot(296)、cursor(296)、kimi(297)、qwen(441)、reasonix(300)、zeroclaw(300)、openclaw(302)、hermes(220)、kiro-cli(0 行，无 src 目录) | 无 |

### 2.2 协议族覆盖（multica vs orchestratord 矩阵）

multica 通过 `agent.go:312 SupportedTypes` 与 `builtin_runtimes.go` 表达两层身份：

- `protocol_family`（= backend 协议种类，受 CHECK 约束）：26 个
- `runtime_id`（= CLI 真实身份，多对一映射到 family）：每个 family 至少 1 个，omp 是 builtin runtime → pi family

orchestratord 仅一个 `backend` 名字 + entry-points + capabilities，没有 protocol_family / runtime_id 分离（草案 §8.4 计划补）。

| multica family | multica 文件（行数） | orchestratord backend | 差距 |
| --- | --- | --- | --- |
| claude | `claude.go` (+cancel/context/deadlock/models test) | `orchestratord-claude` (631) | 接近，orchestratord 简化了 native cancel 与 OAuth 路径 |
| codex | `codex.go` (+app-server test) | `orchestratord-codex` (987) — 自带 app-server 探测 + Cli 退化 | 接近 |
| copilot | `copilot.go` + `copilot_invocation.go` (含 unix/windows 分支) | `orchestratord-copilot` (296) | 严重 —— multica 拆 invocation、windows/unix 进程组、test 分支 |
| cursor | `cursor.go` + `cursor_invocation.go` + `cursor_execute_unix_test.go` + `cursor_integration_test.go` | `orchestratord-cursor` (296) | 严重 |
| opencode | `opencode.go` (+ session) | `orchestratord-opencode` (594) | 接近 |
| dsh | （multica 在 `dsh.go`） | `orchestratord-dsh` (1440) | orchestratord 更深（含 SDK 包装 + agent.cordis 补丁） |
| hermes | `hermes.go` | `orchestratord-hermes` (220) | 接近（都简单） |
| kimi | `kimi.go` | `orchestratord-kimi` (297) | 接近 |
| qwen | `qwen.go` (413) + `qwen_invocation.go` + windows 条件分支 + `_stdin_windows_test.go` | `orchestratord-qwen` (441) | 接近 |
| **codebuddy** | `codebuddy.go` + discovery fallback test | ❌ 无 | 完全缺失 |
| **codearts** | `codearts.go` + cancel/integration test + windows cancel test | ❌ 无 | 完全缺失 |
| **deveco** | `deveco.go` | ❌ 无 | 缺失 |
| **openclaw** | `openclaw.go` | `orchestratord-openclaw` (302) | 接近 stub |
| **pi / omp** | `pi.go` (大量) + `builtin_runtimes.go` 加 `omp` 派生 | ❌ 无 | 缺失（pi 协议族 + oh-my-pi fork） |
| **antigravity (agy)** | `antigravity.go` + test | ❌ 无 | 缺失 |
| **qoder / qoderclicn** | `qoder.go` (449) + 1041 行 test | ❌ 无 | 缺失 |
| **traecli** | `traecli.go` (448) + integration test | ❌ 无 | 缺失 |
| **grok** | `grok.go` | ❌ 无 | 缺失 |
| **qwenpaw** | `qwenpaw.go` (369) + integration test | ❌ 无 | 缺失 |
| **mcode** | `mcode.go` | ❌ 无 | 缺失 |
| **dim** | `dim.go` | ❌ 无 | 缺失 |
| **zeroclaw** | `zeroclaw.go` (603) + 840 行 test | `orchestratord-zeroclaw` (300) | 严重（multica 深度是 orchestratord 的 3 倍） |
| **reasonix** | `reasonix.go` (804) + effort/execute/test | `orchestratord-reasonix` (300) | 严重 |
| **kiro** | `kiro.go` | `orchestratord-kiro-cli` (0 行，无 src) | 完全缺失 —— 仅 pyproject.toml |

**统计：multica 有 9 个 orchestratord 完全缺失的 protocol family（codebuddy / codearts / deveco / pi / antigravity / qoder / qwenpaw / grok / mcode / dim / kiro），共占 26 个 family 的 ~42%。**

### 2.3 协议适配抽象

multica 已经做的、orchestratord 草案计划做但未落的：

| 抽象 | multica | orchestratord |
| --- | --- | --- |
| **ACP 通用适配** | `acp_deliverable.go`、`acp_effort.go`、`acp_terminal.go`、`acp_usage.go` —— 把 ACP（Agent Client Protocol）的 deliverable / effort / terminal / usage 全部抽出来 | 草案 §8.3 计划抽 `orchestratord-acp`（目录存在但仅 811 行，未确认是否真实现 ACP 通用） |
| **Stream-JSON 解析** | `stream_json_result.go` + `stream_scanner.go` + `stream_json_final_output_test.go` —— 通用 stream-json 帧解析，多个 CLI backend 共用 | orchestratord 在每个 backend 里各自解析 |
| **app-server 协议** | `codex.go` 复用 `codex app-server` JSON-RPC | `orchestratord-codex` 已用，但未抽出通用 RPC 适配 |
| **session lock** | `pi_session_lock_unix.go` / `_windows.go` —— POSIX 文件锁 vs Windows 锁 | 无 |
| **proc 组管理** | `proc_windows.go` (295 行) / `proc_other.go` (77 行) —— Windows 进程组取消、POSIX 信号 | 仅 clawcodex/dsh 各自实现 |
| **thinking 抽象** | `thinking.go` (999 行) —— 提取 thinking block 跨 family | 无 |
| **run_collect 生命周期** | `run_collect.go` (501) + `run_collect_quiet.go` + `_lifecycle_test.go` —— 统一的"run 何时算完、什么时候算 quiet" | 无（每个 backend 自己定义 SESSION_COMPLETE） |
| **version 协商** | `version.go` (178 行) + test —— 探测 backend CLI 版本，决定能力位 | 仅 `clawcodex` 有版本断言 |
| **Browser MCP** | `browser_mcp_config.go` —— backend 启动时插入 browser MCP 配置 | 无 |

orchestratord 的 SPI 是更"协议中立"的那一面（`EventEnvelope`、`BackendCapabilities` 8 位、`ApprovalPolicy`、`BackendRunner`），但缺少 multica 这种"在协议层抽出来的可复用基础设施"（ACP、stream-json、proc group、run_collect）。

### 2.4 Capability matrix 报告深度

orchestratord 8 位（`streaming_deltas / resumable / interrupt / approval_hooks / parallel_sessions / cost_reporting / tool_filtering / takeover`）—— 实测 backend 报告：

```
clawcodex 6/8    codex(Cli) 2/8    codex(As) 4/8    dsh 3/8    hermes 2/8
opencode 3/8     cursor 1/8        copilot 1/8       kimi 1/8   qwen 2/8
```

multica 没有显式的"8 位评分"，而是用 `resumeRejectionUndetectable`（6 个 backend）、`BuiltinRuntime`、`protocol_family` 分层 + `acpEffortOptionIDs` / `acpEffortOption` 等专门的可选项 token 字典表达"每个 backend 单独的能力"。两者表达方式不同：orchestratord 把能力抽象为 8 个 bool 位，multica 把能力抽象为 family + per-id 描述符 + per-feature 抽出来。

### 2.5 测试覆盖深度

- orchestratord：`tests/contracts/` 9 个 contract tests + `_backend_cli_registry` 守门 + drift detector。
- multica：78k 行 `server/pkg/agent/` 目录 + `agentintegration` build tag 守门（`MULTICA_RUN_REAL_AGENT_SMOKE=1`） + `scripts/agent-cli-command-names.txt` 默认命令白名单 + `agent_supported_types_test.go` 守门 SupportedTypes 一致性。

### 2.6 Runtime / daemon 抽象缺口（v2 §4.4）

> **v2 补章节**。multica 把"承载 agent 的机器/工具"作为 first-class 抽象 —— **runtime** = 一台电脑 + 该电脑上的一款 AI 编程工具（或自定义运行时配置）（[multica daemon-runtimes 文档](https://multica.ai/docs/zh/daemon-runtimes)）。runtime 与 agent 协议后端是两个独立层次 —— 协议后端决定"用什么 CLI 协议通信"，runtime 决定"在哪台机器、用哪条配置跑这个 CLI"。orchestratord 在协议层做得不错，runtime/daemon 层几乎全缺。

| 维度 | multica | orchestratord v2 |
| --- | --- | --- |
| 守护进程 | Desktop 自动启动；`daemon start` 子命令；PATH 扫描 | 仅 `install.sh` 一次性探测；无运行时扫描 |
| 内置运行时注册 | daemon 启动扫描 `PATH` 中 26 个 CLI；为有权 workspace 注册 runtime | `live_registry.py` 仅进程内；无 PATH 扫描 |
| 自定义运行时配置 | 协议族 + 固定参数 + 命令 / 引号 / 反斜杠转义 + 协议 flag 剔除 + 模型覆盖 | ❌ 无 |
| Runtime 状态 | `ONLINE` / `OFFLINE` / `DISABLED` | ✅ RuntimeStatus enum（`domain/runtime.py:30`）已就位 |
| 心跳 + 离线宽限期 | 15s 心跳；3 分钟内显示离线；7 天无 agent 绑定自动清理 | 30s WS heartbeat 骨架；无宽限期 / 自动清理 |
| Task env（集成契约 5 个，不可覆盖）| `MULTICA_TOKEN` / `MULTICA_TASK_ID` / `MULTICA_AGENT_ID` / `MULTICA_WORKSPACE_ID` / `MULTICA_SERVER_URL` | ❌ 无 env 注入机制 |
| Task env（仅供参考 5+ 个）| `MULTICA_TASK_CONFIG_ROOT` / `MULTICA_TASK_WORKSPACES_ROOT` / `MULTICA_AGENT_NAME` / `MULTICA_DAEMON_PORT` / `MULTICA_TASK_SLOT` / `TMPDIR` 等 | ❌ 无 |
| `workspaces_root` | flag > `MULTICA_WORKSPACES_ROOT` env > profile；修改不迁移 | ❌ 无 workspaces 根概念 |
| 并发上限 | daemon 全局默认 20 + 单 agent 默认 6（取较小）；env `MULTICA_DAEMON_MAX_CONCURRENT_TASKS` | ❌ 无运行时配额 |
| 私有 / 公开 visibility | 私有 = 仅 owner；公开 = 成员可路由但不分享登录凭据 | ❌ 无（单用户模式 D9 下不显现，但数据层需保留） |
| 离线排队恢复 | queue 不失败；宽限期满 + 仍排队满才失败 | ❌ 无 |
| Runtime UI | Multica Desktop 列出 hostname + 在线状态 + 各 CLI 探测结果 + 自定义配置入口 | `packages/views/src/runtimes/runtimes-list.tsx` + `runtimes/{id}/page.tsx` 路由已占位但 stub |

**影响**：orchestratord 当前架构仅支持"开发者在同一台机器上跑 `orchestratord daemon start`"——团队场景（多机器、多 workspace、wrapper 工具、固定版本 CLI）完全未覆盖。这是 multica 形态上最关键的差异化能力之一。v2 主文档 §4.4 已新增对应实施清单。

---

---

## 3. 不属于"可视化 / Agent 支持" 但值得提到的差距

仅做列项，详细分析不在本文档范围：

- **协作抽象**：multica squads / projects / autopilots 已是产品，orchestratord 仅 modes（`single/coordinator/pipeline/debate/swarm`）是"in-process 执行编排"，不是"团队配置抽象"。草案 §7 计划补。
- **多 VCS**：multica `server/internal/integrations/` 含 github / gitlab / gitea / forgejo；orchestratord 仅 `src/orchestratord/linear/` + `src/orchestratord/git/`。
- **通知渠道**：multica `apps/web/app/{slack,lark,dingtalk,wecom,telegram}/` 五件；orchestratord `src/orchestratord/notifications/` 仅 HTTP 桥。
- **Billing**：multica 有 `apps/web/app/billing/`；orchestratord 不做。

---

## 4. 优先级建议（聚焦可视化 + Runtime + Agent）

> **v1 + v2 综合视角的优先级拆分**，与 v2 主文档 §0.4 的 Phase A–E 对应但不等同：
> - 本节"Phase A" ≈ v2 §5 Phase A（Realtime + session control core）+ v2 §5.6 provider tree + v2 §4.4 Runtime 接入
> - 本节"Phase B" ≈ v2 §6 Phase B（通讯层）
> - 本节"Phase C" ≈ v2 §7 Phase C（调度与可观测）+ §9 Phase E
> - 本节"Phase A–C（Agent）" ≈ v2 §8 Phase D
> v2 主文档以单用户模式（D9）下的 Phase A 优先级最高，本文保留 v1 视角便于对照缺口清单；§4.2 Runtime 接入是 v2 周期新增的 §4.4 配套建议。

### 4.1 可视化（按依赖顺序）

| 阶段 | 任务 | 解决缺口 |
| --- | --- | --- |
| Phase A | web provider tree（QueryClient / I18nProvider / ThemeProvider / DashboardGuard） | 当前所有页面裸渲染 |
| Phase A | 复用 multica 的 board / comment / execution-log 三个最大域（issue 详情 + 执行日志 + 评论） | 把 35 个 stub 文件扩展到 100+ 实际组件 |
| Phase B | chat 域 + mention 解析 + WebSocket ↔ TanStack Query 桥接 | 当前完全没有 chat；草案 §5.4 计划 |
| Phase B | runtimes 图表（daily/weekly charts） + inbox 详细组件 | 列表转 dashboard |
| Phase C | settings（VCS / 通知 / 后端启用）、onboarding、command palette、search | 多通道入口 |
| Phase C | i18n 三语化（zh-CN / en / ja） | 当前仅 2 份小字典 |
| (草案外) | Desktop（Electron）+ Mobile（Expo RN） | 草案明确不做；如未来要做，至少复用 `packages/views` 已经会显著降低成本 |

### 4.2 Runtime / daemon 接入（v2 §4.4 配套）

> **v2 周期新增**。multica 的 `daemon-runtimes` 是协议层之外的另一层 first-class 抽象 —— 守护进程 + 一台电脑 + 该电脑上的工具（或自定义运行时配置）。orchestratord v2 周期开始时 `domain/runtime.py` + `live_registry.py` + `runtimes.py` router 已就位（322 行），但 daemon 端 PATH 扫描、Task env 注入、`workspaces_root`、自定义运行时配置、并发上限、私有/公开 visibility 几乎全部未做 —— 这是团队场景能否落地的关键缺口。

| 阶段 | 任务 | 解决缺口 |
| --- | --- | --- |
| **Phase A**（与 §5 同期） | daemon 启动 PATH 探测 + 探测清单打印 + 探测命令输出 | 当前 `install.sh` 仅一次性探测；运行时无重扫能力 |
| **Phase A**（与 §5 同期） | Task env 集成契约 5 个 env var 注入（`ORCHESTRATORD_TASK_ID` / `WORKSPACE_ID` / `AGENT_ID` / `SERVER_URL` / `TOKEN`，命名待讨论是否对齐 multica `MULTICA_*`）| agent CLI 完全感知不到被 orchestrator 调用 |
| **Phase A**（与 §5 同期） | `workspaces_root` 三级覆盖（flag > env > profile）；修改不迁移 | 当前 task 工作目录散落 |
| **Phase A**（与 §5 同期） | daemon 全局并发默认 20（env `ORCHESTRATORD_DAEMON_MAX_CONCURRENT_TASKS` 可调）| daemon 无运行时配额保护 |
| **Phase A**（与 §5 同期） | 自定义运行时配置（协议族 + 命令字段 + 引号/反斜杠转义 + 协议 flag `-p` / `--output-format` / `--input-format` / `--permission-mode` 剔除 + 模型覆盖；参数禁管道/重定向/`&&`/`;`/反引号/env 展开） | 企业部署的核心场景（团队内部 wrapper / 固定版本 CLI） |
| Phase D 同期 | 私有/公开 `visibility` 字段（数据层 alembic migration，单用户模式 D9 下 UI 不显现）| 多用户切换时直接启用 |
| Phase C 同期 | 离线排队恢复 + 7 天无绑定自动清理 | 依赖调度器落地 |
| (草案外) | Multica Desktop 风格自动启动守护进程 | 单用户模式 + Web 一体化后评估 |

### 4.3 Agent（按数量 / 难度比）

| 阶段 | 任务 | 收益 |
| --- | --- | --- |
| Phase A | 落地 `orchestratord-acp` 通用包，**至少覆盖 3 个 ACP backend**（草案已规划） | 一次投入解锁 7 个 ACP backend（codebuddy / deveco / qoder / qoderclicn / qwenpaw / mcode / dim） |
| Phase A | 把 `protocol_family` 与 `runtime_id` 分离（草案 §8.4） | 不用新增 backend 也能接 omp / pi fork |
| Phase A | 把 kimi / copilot / cursor / reasonix / zeroclaw 的 stub 补成真 session 翻译（参照 multica 各自的 test 与 invocation 拆分） | 5 个 P3 backend 立刻可用 |
| Phase B | 补 kiro-cli（仅 pyproject 无 src）、antigravity、grok | 3 个 multica 高频 |
| Phase B | stream-json 通用解析器（参照 multica `stream_json_result.go`） | qwen / pi / opencode / clawcodex 共用 |
| Phase C | run_collect 生命周期抽象 + proc 组管理 | daemon 端 cancel / 进程组清理一致性 |
| Phase C | antigravity / codearts / traecli 私有协议 | 高难度，按用户需求补 |

### 4.4 单点提醒

- orchestratord `packages/views` 的 14 个 domain stub 已经摆好，意味着可视化补全是"补组件深度"而不是"建路由"。
- orchestratord `backends/` 已经摆好 15 个目录（含 stub），意味着 Agent 补全是"补协议翻译"而不是"建包结构"。
- orchestratord `domain/runtime.py` + `live_registry.py` + `runtimes.py` 已就位（322 行），意味着 Runtime 接入补全是"补 daemon 端能力"而不是"建实体层"。
- 真正的瓶颈是 **ACP 通用适配器** + **Runtime 接入**双轨：ACP 解锁 7 个 backend，Runtime 接入让 orchestrator 从"开发者玩具"走向"团队平台"。两条线都得在 Phase A 同期落地，否则 Phase A 的 session control 与 Phase D 的后端覆盖都没有运行环境。

---

## 5. 附录：实测数据快照

### 5.1 文件 / 行数对照

```
orchestratord/apps/web/app/[workspaceSlug]/(dashboard)/        14 个 .tsx/.ts
orchestratord/packages/views/src/                              35 个 .tsx/.ts
multica/apps/web/app/[workspaceSlug]/(dashboard)/              28 个 .tsx/.ts
multica/packages/views/src/                                  1050 个 .tsx/.ts
multica/server/pkg/agent/                                  78,870 行 Go
orchestratord/cli/dashboard.py                              1,464 行 Python（旧 LiveView 单文件）
orchestratord/src/orchestratord/spi/                       5 个 .py（AgentBackend / Session / Capabilities / EventEnvelope / ApprovalPolicy）
```

### 5.2 Agent backend 实测深度

```
orchestratord 真实"硬实现" backend 行数（含 tests）：
  clawcodex  1653 LOC   codex   987 LOC   dsh    1440 LOC
  opencode    594 LOC   claude   631 LOC   hermes  220 LOC
  qwen        441 LOC   （其余 copilot/cursor/kimi/openclaw/reasonix/zeroclaw 均 296~302 行薄壳）
  kiro-cli      0 LOC（仅有 pyproject）
orchestratord-acp：811 LOC（是否真实现 ACP 通用未确认）
```

### 5.3 multica agent 实现深度抽样

```
claude.go + 4 个 _test.go（cancel / context / deadlock / models）
codex.go + cleanup_unix_test.go
cursor.go + 4 个 _test.go（含 invocation unix/windows）
qoder.go (449) + 1041 行 test
antigravity.go + test
pi.go (含 session_lock / stdin / test)
qwen.go (413) + invocation 分 unix/windows + stdin windows test
reasonix.go (804) + effort / execute / test
zeroclaw.go (603) + 840 行 test
grok / kiro / deveco / mcode / dim / codearts / codebuddy / traecli / qwenpaw 均有独立文件
```

### 5.4 引用文档

- 配套主文档：`/mnt/c/WorkSpace/orchestratord/docs/FEATURE_GAP_VS_MULTICA.md`（v2，Phase A–E 路线 + 单用户模式 + 26 family 清单 + 协议基础设施后置清单）
- orchestratord README：`/mnt/c/WorkSpace/orchestratord/README.md`（能力矩阵、事件流表、安装表）
- multica CLAUDE.md：`/mnt/c/WorkSpace/multica/CLAUDE.md`（State Rules、Package Boundaries、Sharing Rules、Testing 守门）
- multica SupportedTypes：`/mnt/c/WorkSpace/multica/server/pkg/agent/agent.go:312`
- multica BuiltinRuntimes：`/mnt/c/WorkSpace/multica/server/pkg/agent/builtin_runtimes.go:87`

---

**变更记录**

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v1 | 2026-09-07 | 初稿；可视化与 Agent 两个维度的实测对比；新增详细阶段建议 |
| v2 | 2026-09-07 | 同步 v2 主文档：§0、§4 heading、§5.4 引用指向 v2；保留本文档为 v2 的量化基线 + 逐视图/逐 family 拆解附录 |