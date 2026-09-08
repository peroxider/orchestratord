# orchestratord Next.js Web 前端设计需求文档

> 状态：Draft v1
> 日期：2026-09-08
> 范围：`apps/web`、`packages/ui`、`packages/views`、相关 `packages/core` Web 适配
> 产品形态：单用户、Web-only、本地优先的 AI Agent 编排控制台
> 参考产品：Multica Web Dashboard（仅参考设计原则、信息架构和交互模式，不直接派生其 UI 代码）

---

## 0. 文档目的

本文定义 orchestratord Next.js Web 前端从当前功能骨架升级为产品级控制台所需的完整设计要求，包括：

- 产品定位与目标用户；
- 单用户模式边界；
- 信息架构与路由；
- 全局 Shell 与页面结构；
- 视觉语言、设计 Token 和组件体系；
- 各业务页面的功能、布局、状态和交互；
- 实时更新、错误、空状态与反馈；
- 响应式、无障碍、性能和测试要求；
- Multica 参考边界及许可风险；
- 分阶段实施计划和验收标准。

本文是前端产品设计与实现的约束文档，不替代现有后端协议、Agent SPI、执行引擎和数据库设计文档。

---

## 1. 执行摘要

### 1.1 核心结论

orchestratord 应参考 Multica Dashboard 的成熟度、信息密度和页面组织方式，但不复制 Multica 的界面代码，也不继承其登录、用户、成员、邀请、Workspace 切换、Seat 或 Billing 机制。

目标界面不是通用项目管理 SaaS，而是：

> 面向单一操作人的 Agent 任务编排与运行控制台。

前端需要围绕三个最重要的问题组织：

1. 现在有哪些任务和 Agent 正在运行？
2. 哪些执行被阻塞，需要操作人处理？
3. 如何查看执行依据，并安全地批准、暂停、恢复、重试或停止？

### 1.2 设计概念

视觉概念命名为 **Execution Ledger（执行账本）**。

- “Ledger”代表可靠、可追溯、按时间与因果关系组织的信息。
- 界面整体克制、安静、紧凑，不使用营销式渐变、装饰性大卡片或无意义数据墙。
- 唯一重点视觉元素是 **Execution Spine（执行轨道）**：在 Session、Issue Detail 和 Overview 中，以统一语法表达阶段、Agent、工具调用、等待、阻塞和结果。
- 状态颜色只表达语义，不承担装饰职责。

### 1.3 最高优先级

1. 去除浏览器登录和多用户产品表面。
2. 统一隐藏 Workspace 的识别与数据上下文。
3. 建立 UI primitives、设计 Token 和 Dashboard Shell。
4. 优先升级 Overview、Inbox、Issues、Session Detail。
5. 再升级 Chat、Agents、Runtimes、Skills、Usage 和 Activity。

---

## 2. 背景与当前实现评估

### 2.1 已有能力

当前实现已经具备以下基础，不应推倒重写：

- Next.js 16 App Router；
- React 19；
- TanStack Query 管理服务端状态；
- Zustand 管理客户端状态；
- REST API Client；
- WebSocket realtime bridge；
- Issues List 和 Kanban；
- `@dnd-kit` 拖拽；
- Inbox approval / clarification / failure 分类；
- Chat session 与消息时间线；
- Session pause / resume / stop / approval 控制；
- pipeline / debate / swarm / coordinator 模式可视化；
- Agents capability matrix；
- Skills、Runtimes、Autopilots、Projects、Squads、Usage、Audit 等视图骨架；
- light / dark token；
- en / zh-CN / ja i18n 基础。

### 2.2 当前体验缺口

当前主要问题是页面组织和产品一致性，而不是路由数量：

- 根页仍是简单标题和登录链接；
- Dashboard 没有正式侧边栏和统一页面标题栏；
- 页面缺少全局搜索、新建入口、连接状态和未读状态；
- UI primitives 数量不足；
- 大量页面样式集中在全局 CSS；
- 页面之间的 gutter、标题、toolbar、empty/error/loading 语法不一致；
- 部分页面使用解析后的 Workspace UUID，部分仍直接使用 URL slug；
- 登录默认关闭，但登录页、AuthGate、Token Storage 和 member identity 仍保留；
- Members 页面和成员角色语义与单用户产品定位冲突；
- Session 可视化是产品差异点，但目前没有一级 Sessions 列表或总览入口。

### 2.3 设计重构原则

- 保留现有 core/query/realtime 能力；
- 保留 orchestratord 独有的编排模式；
- 页面组件按业务域逐步升级；
- 不以复制 Multica 整包组件为捷径；
- 不在同一阶段同时重写后端、状态管理和视觉层；
- 新旧页面不得长期并存为两套 UI；
- 产品未正式发布时，优先移除旧路径，不建设永久兼容层。

---

## 3. 产品定义

### 3.1 产品定位

orchestratord Web 是本地优先的 AI Agent 编排控制台，用于：

- 创建和管理工作项；
- 将工作交给 Agent 或 Agent Squad；
- 观察多 Agent 执行过程；
- 处理审批、澄清和失败；
- 控制长时间运行的 Session；
- 管理 Agent、Runtime、Skill 和 Autopilot；
- 查看使用量、活动和执行证据。

### 3.2 目标用户

主要用户：

- 在个人电脑、开发服务器或私有环境运行多个编码 Agent 的开发者；
- 需要集中查看 Agent 工作状态的技术负责人；
- 需要批准工具调用、检查执行记录和定位失败的本地操作人。

不以以下用户为当前目标：

- 需要组织级成员管理的企业管理员；
- 需要 Seat、账单和订阅管理的 SaaS 客户；
- 仅浏览营销网站的公众访客；
- 需要移动端原生体验的用户。

### 3.3 用户核心任务

| 优先级 | 用户任务 | 成功标准 |
| --- | --- | --- |
| P0 | 查看需要处理的阻塞 | 进入应用后 5 秒内识别待审批、待澄清和失败项 |
| P0 | 查看当前执行 | 能看到活跃 Session、阶段、Agent、耗时和最新动作 |
| P0 | 控制执行 | Pause / Resume / Stop / Approve / Deny 均有明确反馈和最终状态 |
| P0 | 创建工作项 | 在任意主页面不超过两步创建 Issue |
| P1 | 检查执行证据 | 从 Issue 或 Session 进入完整 timeline、tool call 和结果 |
| P1 | 管理 Agent 能力 | 能查看 Agent、backend、runtime、skill 和 capability |
| P1 | 定位异常 | 能从 Inbox、Activity、Runtime 进入具体失败对象 |
| P2 | 分析用量 | 能按时间、Agent、Backend、Project 查看 token 和成本 |

---

## 4. 范围与非目标

### 4.1 本期范围

- 单用户 Web Dashboard；
- 桌面浏览器和移动浏览器响应式；
- light / dark theme；
- 中文、英文、日文；
- Overview、Inbox、Chat、Issues、Sessions、Agents、Squads、Autopilots、Projects、Runtimes、Skills、Usage、Activity；
- 全局搜索与命令面板；
- 实时连接状态；
- 完整 loading / empty / error / offline / permission-like capability degradation 状态；
- 键盘操作和基础无障碍；
- Playwright 关键路径和视觉回归。

### 4.2 明确不做

- 用户注册、登录、登出；
- 成员管理；
- Owner / Admin / Member 角色；
- Workspace 创建和切换；
- 邀请、Join、Seat；
- Billing、Subscription、Entitlement；
- 公开营销主页；
- Desktop Electron；
- Mobile Native；
- 与 Multica UI 代码形成直接派生关系；
- 为未来多用户模式提前保留隐藏按钮或未完成页面。

### 4.3 保留但不暴露的内部概念

- 数据库 `workspace_id`；
- 固定 default workspace；
- 数据隔离过滤；
- audit actor；
- daemon/runtime credential；
- token hash 和 rotate；
- 后端为未来迁移保留的多租户字段。

这些是内部数据边界，不得出现在主导航、URL、页面标题或用户操作中。

---

## 5. 单用户模式需求

### 5.1 浏览器身份

- 浏览器不提供登录页面；
- 浏览器不保存用户 Bearer Token；
- 浏览器 API Client 不附加用户 Authorization；
- WebSocket 不传浏览器用户 Token；
- 根路径直接进入应用；
- 前端不得要求 member ID 才能评论、审批或创建 Issue。

### 5.2 本地操作人

需要记录人类操作时，统一使用后端推导的 `local_operator`：

- 前端不上传 `author_id`；
- 前端不上传 `member_id`；
- 后端为操作生成稳定 actor；
- UI 显示“你”或“本地操作人”；
- Activity 可以显示 actor 类型，但不显示角色或权限。

### 5.3 隐藏 Workspace

推荐增加 `/api/instance` 或 `/api/bootstrap`：

```json
{
  "instance_name": "orchestratord",
  "workspace_id": "uuid",
  "workspace_name": "Local",
  "server_version": "x.y.z",
  "realtime_url": "ws://...",
  "features": {}
}
```

要求：

- 应用启动时只请求一次；
- `workspace_id` 注入 Query 和 mutation；
- URL 不包含 workspace slug；
- 页面组件只从 Instance Context 读取 workspace ID；
- 不允许页面自行从 path 解析 workspace；
- bootstrap 失败时显示全屏诊断状态。

### 5.4 安全边界

无浏览器登录时：

- 服务默认监听 `127.0.0.1`；
- UI 明确显示当前连接目标；
- 非 loopback 暴露必须在文档中警告；
- 远程访问推荐 VPN 或反向代理鉴权；
- destructive execution control 必须二次确认；
- daemon/runtime token 机制不得删除；
- Agent CLI 仍只能通过 daemon 中转。

---

## 6. 信息架构与路由

### 6.1 主导航结构

```text
Attention
  Overview
  Inbox
  Chat

Work
  Issues
  Projects

Orchestration
  Sessions
  Agents
  Squads
  Autopilots

System
  Runtimes
  Skills
  Usage
  Activity
```

导航分组表达真实业务关系，不使用纯装饰性编号。

### 6.2 目标路由

| 路由 | 页面 | 备注 |
| --- | --- | --- |
| `/` | Redirect 或 Overview | 推荐直接渲染 Overview |
| `/overview` | Mission Control | 可选；若 `/` 已渲染则不重复 |
| `/inbox` | Attention Inbox | 单用户统一待办 |
| `/chat` | Chat | Session thread 列表和对话 |
| `/issues` | Issues | List / Board；后续 Table |
| `/issues/[id]` | Issue Detail | 详情、评论、Session、执行轨道 |
| `/projects` | Projects | 项目分组，不含成员 |
| `/projects/[id]` | Project Detail | 项目下 Issue 与活动 |
| `/sessions` | Sessions | 活跃、等待、历史、失败 |
| `/sessions/[id]` | Session Detail | 核心编排页面 |
| `/agents` | Agents | Agent 列表 |
| `/agents/[id]` | Agent Detail | 能力、Runtime、Skill、最近执行 |
| `/squads` | Squads | Agent 组合 |
| `/squads/[id]` | Squad Detail | 角色、成员 Agent、编排规则 |
| `/autopilots` | Autopilots | 自动化列表 |
| `/autopilots/[id]` | Autopilot Detail | 规则、运行、历史 |
| `/runtimes` | Runtimes | 机器与 backend 状态 |
| `/runtimes/[id]` | Runtime Detail | 健康、工具、并发、用量 |
| `/skills` | Skills | Skill catalog |
| `/skills/[name]` | Skill Detail | 文件、来源、验证状态 |
| `/usage` | Usage | token、成本和时长 |
| `/activity` | Activity | 取代面向用户的 Audit 命名 |

### 6.3 删除路由

- `/login`
- `/members`
- `/{workspaceSlug}/*`
- 所有 workspace 创建、邀请、加入、账户和 Billing 路由

产品未发布时直接删除旧路由。若已存在外部书签，可提供一个发布周期的服务端 redirect，但不得维护双套页面。

---

## 7. 全局 Dashboard Shell

### 7.1 桌面结构

```text
┌──────────────────────┬──────────────────────────────────────────────┐
│ Brand / Instance     │ Page header                                 │
│                      │ title · context · search · connection · CTA │
│ Navigation           ├──────────────────────────────────────────────┤
│                      │ Optional toolbar                             │
│                      ├──────────────────────────────────────────────┤
│                      │                                              │
│                      │ Page canvas                                  │
│                      │                                              │
│ Runtime summary      │ Optional detail panel / modal                │
└──────────────────────┴──────────────────────────────────────────────┘
```

### 7.2 Sidebar

- 展开宽度：248px；
- 折叠宽度：56px；
- `xl` 以上默认展开；
- `md` 到 `xl` 默认折叠；
- `md` 以下使用 modal Sheet；
- 展开/折叠偏好持久化；
- 活跃项在 hover 时仍保持可识别；
- 导航图标来自统一 route icon map；
- Inbox 显示未处理数量；
- Sessions 显示活跃数量；
- Runtime 断连时在 System 分组显示状态点；
- Sidebar 底部只放实例状态和帮助入口，不放账户菜单。

### 7.3 顶部页面栏

- 高度：48px；
- 所有页面使用相同 gutter；
- 左侧：移动端 Sidebar Trigger、页面图标、标题、可选数量；
- 中部：可选上下文或 breadcrumb；
- 右侧：搜索、连接状态、页面主操作；
- Detail 页面使用一致 breadcrumb；
- 标题和操作不得因页面切换产生水平跳动。

### 7.4 全局操作

- `Cmd/Ctrl + K`：全局搜索与命令面板；
- `C` 或配置快捷键：新建 Issue；
- `G` 前缀：导航；
- `Esc`：关闭当前最高层 modal/popover；
- 连接状态按钮：展开 REST、WebSocket、Runtime 诊断摘要；
- 所有快捷键必须在帮助面板中可发现。

---

## 8. 视觉系统

### 8.1 视觉方向

目标是“运行控制台的精确感”，不是传统企业后台，也不是科幻 HUD。

需要避免：

- 大面积渐变；
- 霓虹描边；
- 每个区块都使用 Card；
- 装饰性统计数字；
- 无意义玻璃拟态；
- 所有图标都有彩色背景；
- 为显得“AI”而加入粒子、光晕和持续动画；
- 直接复制 Multica 营销页的蓝色场景和 serif hero。

### 8.2 核心调色板

#### Dark 默认主题

| Token | 名称 | 值 | 用途 |
| --- | --- | --- | --- |
| `--app-shell` | Carbon shell | `#0D1016` | Sidebar 与应用外壳 |
| `--page-canvas` | Graphite canvas | `#121721` | 页面底色 |
| `--surface` | Slate surface | `#181E29` | 行、面板、卡片 |
| `--surface-raised` | Raised steel | `#212938` | Popover、Dialog、浮层 |
| `--foreground` | Cold paper | `#EEF3FA` | 主文字 |
| `--signal` | Execution blue | `#6AA9FF` | 活跃、链接、执行中 |

#### Light 主题

| Token | 名称 | 值 |
| --- | --- | --- |
| `--app-shell` | Fog shell | `#EEF1F5` |
| `--page-canvas` | Work canvas | `#F7F8FA` |
| `--surface` | Paper surface | `#FFFFFF` |
| `--surface-raised` | Raised paper | `#F1F4F8` |
| `--foreground` | Graphite ink | `#161B22` |
| `--signal` | Execution blue | `#2563EB` |

### 8.3 状态颜色

| 语义 | Dark | Light | 使用规则 |
| --- | --- | --- | --- |
| Success / completed | `#49B675` | `#168449` | 完成、在线、通过 |
| Warning / waiting | `#D5A447` | `#9A6700` | 等待、退化、注意 |
| Destructive / failed | `#F06A6A` | `#CF2E2E` | 失败、停止、拒绝 |
| Review / approval | `#B18CFF` | `#7652C7` | 审批、judge、人工检查 |
| Neutral / idle | `#8B95A5` | `#667085` | 空闲、暂停、未知 |

状态不能只靠颜色表达，必须同时使用图标、文字或形状。

### 8.4 字体

- Display / Page title：Manrope，600；仅用于 Overview 标题和一级页面标题；
- UI / Body：Inter，400 / 500 / 600；
- Data / IDs / Commands：Geist Mono，400 / 500；
- 中文 fallback：`PingFang SC`, `Microsoft YaHei`, `Noto Sans CJK SC`；
- 日文 fallback：`Hiragino Sans`, `Yu Gothic`, `Noto Sans CJK JP`；
- 不允许使用浏览器默认 CJK 字体顺序处理日文汉字。

### 8.5 文字尺度

| 角色 | Size / Line height | 用途 |
| --- | --- | --- |
| Display | 32 / 38 | Overview 关键标题，仅一处 |
| Title large | 20 / 28 | 特殊详情标题 |
| Title | 18 / 26 | 页面或 Dialog 标题 |
| Title small | 16 / 24 | Section 标题 |
| Body large | 15 / 22 | 详情正文 |
| Body | 14 / 20 | 默认 UI 文本 |
| Label | 13 / 18 | 表单、紧凑行 |
| Caption | 12 / 16 | 元数据、时间、辅助说明 |
| Micro | 11 / 15 | 轨道编号、图表轴、辅助标记 |

不得在页面中使用随意的 `13px/15px/17px` 局部字号。

### 8.6 空间与圆角

- 基础空间单位：4px；
- 常用间距：4 / 8 / 12 / 16 / 24 / 32；
- 页面 gutter：16px；宽屏阅读型页面最大 24px；
- 控件高度：32px 默认，36px 表单主控件，40px 仅移动端触控；
- 圆角：6px 控件、8px surface、10px dialog；
- Pill 仅用于状态和紧凑筛选，不用于普通按钮；
- 优先使用留白区分 section，必要时才使用 divider。

### 8.7 阴影与边框

- 页面内容主要依靠 surface 层级和 1px border；
- 普通 Card 不使用明显阴影；
- Popover、Dialog、拖拽浮层允许轻阴影；
- focus ring 必须清晰；
- selected、hover、active 是三个独立状态；
- selected 状态不得在 hover 时退化成普通 hover。

### 8.8 图标

- 使用 Lucide；
- 默认 16px；
- 页面标题图标 16px；
- 空状态图标 32–40px；
- 图标线宽保持一致；
- 相同动作始终使用相同图标；
- 不为每个导航项目设计品牌插画。

---

## 9. Execution Spine 设计规格

### 9.1 目的

Execution Spine 是产品的视觉签名，用于统一展示一次 Session 的执行因果链。

### 9.2 基本单位

每个节点包含：

- 顺序或时间；
- Agent / System / Local operator；
- 事件类型；
- 简短摘要；
- 状态；
- 可选输入输出预览；
- 可选耗时、token、cost；
- 可展开证据。

### 9.3 节点类型

- Prompt / handoff；
- Agent started；
- Reasoning summary；
- Tool call；
- Tool result；
- Approval requested；
- Clarification requested；
- Pause / resume / stop；
- Subtask created；
- Retry；
- Error；
- Final result。

### 9.4 模式表现

- Single：单条纵向轨道；
- Pipeline：stage 间明确 handoff；
- Debate：proposer 并列，judge 汇合；
- Swarm：按 wave 分组，子任务形成树；
- Coordinator：按 Agent lane 呈现时间区间；
- 未知模式：降级为通用 timeline，不得崩溃。

### 9.5 动效

- 新事件抵达时使用 160–220ms 淡入和位移；
- 当前运行节点可使用低频 opacity pulse；
- 不允许整条轨道持续流光；
- `prefers-reduced-motion` 下禁用位移和 pulse；
- 用户查看历史位置时不得强制自动滚到底部；
- 用户已位于底部附近时才自动跟随。

---

## 10. 公共组件需求

### 10.1 UI primitives

第一阶段必须具备：

- Button；
- IconButton；
- Badge / StatusBadge；
- Input / Textarea；
- Select；
- Checkbox / Switch；
- Tabs / SegmentedControl；
- Tooltip；
- DropdownMenu；
- Popover；
- Dialog / AlertDialog；
- Sheet；
- Command；
- Toast；
- Skeleton；
- EmptyState；
- Spinner；
- Progress；
- Table；
- ScrollArea；
- Collapsible；
- ResizablePanel；
- Kbd；
- Separator。

推荐使用 shadcn + Base UI 或 Radix 风格 primitives，但必须映射到本项目 semantic tokens。

### 10.2 业务公共组件

- AppSidebar；
- PageHeader；
- BreadcrumbHeader；
- PageToolbar；
- CollectionPageHeader；
- PageState；
- ConnectionStatus；
- StatusPill；
- ActorAvatar；
- AgentBadge；
- BackendBadge；
- RuntimeStatus；
- IssueChip；
- SessionChip；
- Duration；
- TokenCount；
- Cost；
- RelativeTime；
- CodeBlock；
- ToolCallCard；
- ToolResultCard；
- TranscriptDialog；
- ConfirmDestructiveAction。

### 10.3 状态组件统一规则

每个数据页面必须覆盖：

- 初始加载；
- 后台刷新；
- 空数据；
- 可恢复错误；
- 不可恢复错误；
- 离线；
- realtime reconnecting；
- partial data；
- capability unavailable；
- mutation pending；
- mutation failed；
- mutation success。

初始加载使用结构化 Skeleton，不使用单行 “Loading…”；后台刷新不得清空已有内容。

---

## 11. 页面需求

### 11.1 Overview / Mission Control

#### 页面目标

让操作人在 5 秒内知道当前系统状态和下一步行动。

#### 页面结构

1. Attention Strip
   - pending approvals；
   - clarifications；
   - failed sessions；
   - offline runtimes。
2. Active Sessions
   - 最多展示 6 个；
   - 显示模式、Agent、当前节点、耗时和控制操作。
3. Recent Work
   - 最近更新 Issue；
   - 最近完成 Session。
4. Runtime Health
   - 在线机器；
   - 可用 backend；
   - 当前并发与上限。
5. Usage Pulse
   - 今日 token；
   - 今日成本；
   - 只做简要趋势，不做装饰性大数字墙。

#### 空状态

首次启动时显示明确流程：

1. 连接或发现 Runtime；
2. 创建 Agent；
3. 创建第一个 Issue。

不得引导注册、创建 Workspace 或邀请成员。

### 11.2 Inbox

#### 页面目标

集中处理所有需要人类行动的事项。

#### 分类

- Approval request；
- Clarification；
- Failed execution。

#### 功能

- All / Approval / Clarification / Failed 筛选；
- 按新旧排序；
- 展开上下文；
- 进入相关 Issue 或 Session；
- Approve / Deny；
- Reply；
- Retry；
- Dismiss。

#### 单用户调整

- 删除 “Assign to me”；
- 删除 assignee member；
- 所有未处理项默认属于本地操作人；
- 已处理项保留 actor 与时间。

### 11.3 Chat

#### 页面目标

通过持续会话给 Agent 下达任务并查看实时结果。

#### 桌面布局

- 左：Session thread list，240–280px；
- 中：Message timeline；
- 右：可选 context panel，280–360px；
- 右栏默认关闭，可按 Issue、Project、Agent 上下文打开。

#### 功能

- 新建 Chat；
- Session rename；
- 搜索历史会话；
- pending message；
- resend；
- markdown / code / tool call；
- attachment；
- stop generating；
- 跳转 Session Detail。

#### 行为

- 发送后消息立即显示 pending；
- 失败消息保留并允许 retry；
- realtime 更新同一 Query cache；
- 不使用静默 optimistic success；
- 草稿按 Session 隔离并持久化。

### 11.4 Issues

#### 页面目标

创建、筛选、排序和推进工作项。

#### 第一阶段模式

- List；
- Board。

#### 后续模式

- Table；
- Timeline 或 Swimlane，只有实际需求明确后再实现。

#### Header / Toolbar

- 页面标题和总数；
- View switcher；
- Search；
- Status、Project、Agent、Label 筛选；
- Sort；
- New issue。

#### List Row

- Identifier；
- Title；
- Status；
- Priority；
- Project；
- Agent / Squad；
- 活跃执行指示；
- 更新时间。

#### Board Card

- Title；
- Identifier；
- Priority；
- Agent；
- Project；
- Label，最多显示 3 个；
- 正在执行状态；
- 长标题最多 3 行。

#### 拖拽

- Pointer 和 keyboard sensor；
- 拖拽开始后显示 overlay；
- 目标列高亮；
- optimistic update；
- 失败回滚并 Toast；
- realtime authoritative event 最终校准；
- 移动端提供菜单式状态修改作为替代。

### 11.5 Issue Detail

#### 页面结构

- Breadcrumb header；
- 主内容：标题、描述、评论与活动；
- 属性栏：Status、Priority、Project、Agent/Squad、Labels；
- Execution section：相关 Sessions 和当前执行轨道；
- 可选右侧详情 panel。

#### 单用户调整

- 评论作者显示“你”；
- 不显示订阅成员、关注者或成员头像组；
- Assignee 仅 Agent、Squad、Unassigned；
- 删除 member mention；
- `@` 只补全 Agent 和系统实体。

### 11.6 Sessions List

#### 页面目标

成为所有 Agent 执行的一级入口。

#### 分区

- Active；
- Waiting for input；
- Queued；
- Failed；
- Completed。

#### 每行字段

- Session ID；
- Issue / Chat 来源；
- Mode；
- Agent / Squad；
- 当前阶段；
- 状态；
- 开始时间；
- 耗时；
- token / cost；
- 快速控制。

#### 筛选

- Status；
- Mode；
- Agent；
- Backend；
- Runtime；
- Date range。

### 11.7 Session Detail

#### 页面目标

提供执行控制、实时观察和事后审计。

#### Header

- Session ID；
- 状态；
- Mode；
- 来源 Issue / Chat；
- Agent / Squad；
- Runtime；
- 开始时间和耗时；
- Pause / Resume / Stop；
- Open transcript。

#### 主内容

- 当前审批或澄清固定置顶；
- Execution Spine；
- mode-specific visualization；
- tool call / result 展开；
- token、cost、duration 汇总；
- 最终结果和失败原因。

#### destructive 行为

- Stop 需要确认；
- 明确说明是否会终止子进程；
- pending 时禁用重复操作；
- 请求成功不代表执行已停止，必须等待 authoritative event；
- 超时后显示“停止请求已发送，仍在等待 Runtime 响应”。

### 11.8 Agents

#### 列表

- Name；
- Backend；
- Runtime；
- Status；
- Capability summary；
- Attached skills；
- Active sessions；
- Last activity。

#### 详情

- 基础配置；
- Instructions；
- Model / backend；
- Runtime binding；
- Capability matrix；
- Skills；
- Concurrency；
- Recent sessions；
- Failure history。

#### 单用户调整

- 不显示 owner；
- 不显示成员访问范围；
- 不显示 private/public visibility；
- 不显示成员可见性或共享设置。

### 11.9 Squads

Squad 被定义为 Agent 编排单元，不是人类团队。

- Squad 名称和说明；
- Agent members；
- Coordinator；
- Mode；
- Handoff 规则；
- Concurrency；
- Attached skills；
- 最近执行。

所有 `member_type=member` 相关创建选项从 UI 移除。

### 11.10 Autopilots

- Name；
- Trigger；
- Target Issue template / Agent / Squad；
- Schedule；
- Enabled；
- Next run；
- Last result；
- Failure count；
- Run now；
- Pause；
- Execution history。

Cron 表达式不得作为唯一用户界面；提供自然语言摘要和下一次运行时间。

### 11.11 Projects

Project 是 Issue 分组，不是权限容器。

- Name；
- Description；
- Status；
- Issue counts；
- Active sessions；
- Progress；
- Recent activity。

删除 Project member、lead member 和访问权限概念；如需负责人，只允许 Agent/Squad 或本地操作人。

### 11.12 Runtimes

#### 列表

- Hostname；
- Online / offline；
- Last heartbeat；
- OS / arch；
- 可用 backend；
- 当前并发 / 上限；
- 活跃 Sessions；
- 最近错误。

#### 详情

- Health；
- CLI detection；
- Backend profiles；
- workspaces root；
- concurrency；
- environment summary；
- daily / weekly usage；
- activity heatmap；
- error history。

敏感环境变量的值永不展示，只显示配置是否存在。

### 11.13 Skills

#### 列表

- Name；
- Description；
- Origin；
- Version/hash；
- Verification；
- Attached Agents；
- Last refresh。

#### 详情

- README / SKILL.md；
- File tree；
- Source map；
- Hash / stale state；
- Refresh / verify；
- Attached Agents；
- 使用历史。

stale、missing source 和 hash mismatch 必须有不同提示。

### 11.14 Usage

- 时间范围：7d / 30d / 90d / custom；
- 维度：Agent / Backend / Runtime / Project / Mode；
- 指标：input token、output token、cache read/write、cost、duration、session count、failure count；
- Summary cards 不超过 4 个；
- 趋势图使用一致颜色和轴格式；
- 表格与图表共享筛选；
- 缺少价格时显示“未配置价格”，不得显示 `$0`。

### 11.15 Activity

- 面向用户统一命名为 Activity；
- 底层 API 和数据库可继续使用 Audit；
- 支持时间、事件类型、实体、Agent、Runtime 筛选；
- 每行显示 actor、action、target、time 和结果；
- mutation 前后值按需展开；
- 敏感 token、prompt secret 和环境变量必须脱敏；
- 点击 target 进入相关页面。

---

## 12. 搜索与命令面板

### 12.1 搜索对象

- Issues；
- Sessions；
- Agents；
- Squads；
- Projects；
- Runtimes；
- Skills；
- Commands。

### 12.2 行为

- `Cmd/Ctrl + K` 打开；
- 输入防抖不超过 150ms；
- 最近访问项在空查询时显示；
- 结果按实体类型分组；
- 键盘上下选择、Enter 打开；
- 跳转后自动关闭；
- 搜索失败不能影响导航命令；
- 小屏占满屏幕，大屏为居中 Dialog。

---

## 13. 实时与反馈

### 13.1 连接状态

全局状态至少包括：

- Connected；
- Connecting；
- Reconnecting；
- Offline；
- Degraded；
- Authentication 不作为状态出现。

Reconnecting 超过 5 秒后在页面顶部显示非阻塞提示；恢复后自动消失并提示已同步。

### 13.2 Query 与 WebSocket

- React Query 是服务端状态唯一来源；
- WebSocket 事件 patch 或 invalidate Query cache；
- 不把服务端实体镜像到 Zustand；
- mutation 与 realtime 事件必须处理自发事件重复；
- 页面卸载后不得继续写入本地组件状态；
- reconnect 后执行必要的增量或完整同步。

### 13.3 Toast

Toast 仅用于：

- mutation 成功且页面无其他明显反馈；
- mutation 失败；
- connection recovered；
- copy 成功；
- background action 完成。

不得为每次筛选、导航或实时事件弹 Toast。

### 13.4 错误文案

错误必须说明：

1. 发生了什么；
2. 当前数据是否安全；
3. 用户可以做什么。

示例：

- 推荐：“无法停止 Session。Runtime 当前离线；停止请求尚未确认。重新连接后再试。”
- 禁止：“Something went wrong.”
- 禁止：“抱歉，出现未知错误。”

---

## 14. 响应式需求

### 14.1 Breakpoints

| 范围 | 行为 |
| --- | --- |
| `< 640px` | 手机；Sidebar Sheet；单栏；detail 全屏 |
| `640–767px` | 大手机；紧凑工具栏；次要操作进菜单 |
| `768–1279px` | 平板/小桌面；Sidebar 图标 rail；双栏按需 |
| `>= 1280px` | 完整 Sidebar；支持 detail panel |
| `>= 1536px` | 宽屏；内容宽度增加，但文本阅读列保持上限 |

### 14.2 移动端替代交互

- Board 可横向滚动，但必须提供 List 模式；
- 拖拽必须有菜单式状态修改替代；
- Hover affordance 必须有可点击替代；
- Dialog 在小屏转为 Sheet 或全屏；
- 三栏 Chat 在小屏变为列表 → 对话 → 上下文三级导航；
- PageHeader 主操作保留，次要操作进入 overflow menu；
- 表格允许列裁剪或行卡片化，不能只缩小字体。

---

## 15. 无障碍需求

- 目标：WCAG 2.2 AA；
- 所有功能可键盘完成；
- focus visible 不得被移除；
- Dialog、Popover、Sheet 正确管理 focus trap 和 return focus；
- 图标按钮必须有 accessible name；
- 状态不能只靠颜色；
- 拖拽提供 keyboard sensor 和非拖拽替代；
- 动态事件使用克制的 `aria-live`；
- 高频 realtime 日志不得逐条打断屏幕阅读器；
- 表格表头和排序状态可感知；
- 最低正文对比度 4.5:1；
- 大文字最低 3:1；
- UI 边界和 focus indicator 满足 3:1；
- 尊重 `prefers-reduced-motion`；
- 尊重浏览器文字缩放至 200%；
- 触控目标原则上至少 40×40px，紧凑桌面控件需有足够间距。

---

## 16. 内容与本地化

### 16.1 语言

- `en`；
- `zh-CN`；
- `ja`。

### 16.2 文案原则

- 使用用户理解的对象名称，不暴露内部实现；
- 按钮直接描述动作：“停止 Session”，不用“提交”；
- 同一个动作在按钮、Toast 和 Activity 中使用相同动词；
- 空状态告诉用户下一步；
- 错误文案具体说明恢复方式；
- 不把英文字符串散落在组件中；
- ID、命令、路径和 backend 名称不翻译。

### 16.3 推荐术语

| English | zh-CN |
| --- | --- |
| Issue | 任务 |
| Session | 会话 |
| Runtime | 运行时 |
| Agent | Agent |
| Squad | Agent 小组 |
| Skill | 技能 |
| Autopilot | 自动任务 |
| Approval | 审批 |
| Clarification | 澄清 |
| Activity | 活动 |

最终术语需与现有文档统一后再锁定，避免同一界面混用“任务/task/Issue”。

---

## 17. 前端架构要求

### 17.1 包职责

#### `apps/web`

- Next.js route；
- Web platform adapter；
- metadata；
- environment config；
- browser-only integration；
- route-level loading/error boundary。

#### `packages/core`

- API client；
- schemas；
- TanStack Query options/hooks；
- realtime；
- Zustand client state；
- instance bootstrap；
- 不依赖具体 UI。

#### `packages/ui`

- semantic tokens；
- primitives；
- 通用无业务 UI；
- 不导入 `@orchestratord/core`。

#### `packages/views`

- Dashboard Shell；
- 页面级业务组件；
- 业务复合组件；
- 不直接导入 `next/*`；
- 通过 Navigation Adapter 导航；
- 不新建服务端实体 Zustand store。

### 17.2 CSS 策略

- Tailwind 用于布局和 semantic utility；
- CSS variables 是主题唯一来源；
- 禁止在业务组件中硬编码颜色；
- 禁止继续扩大单一 `globals.css`；
- rich content、Execution Spine 等复杂域允许独立 CSS；
- 页面组件不得定义重复的 header/gutter 样式；
- specificity 以单类和 data attribute 为主；
- 不使用 `!important` 解决普通状态冲突。

### 17.3 状态边界

- React Query：Issues、Sessions、Agents、Runtimes、Inbox 等服务端状态；
- Zustand：view mode、filter draft、modal、sidebar、草稿、用户偏好；
- URL：页面位置、可分享筛选、选中实体；
- React Context：Instance、Navigation、Theme 等平台 wiring；
- localStorage：主题、Sidebar、草稿、显示偏好；
- localStorage 不存服务端实体或认证 token。

### 17.4 API 边界

- 所有网络响应必须有 schema 解析；
- 新 enum 必须有 unknown fallback；
- 关键布尔字段使用显式判断；
- 错误结构规范化；
- capability 缺失显示降级状态，不隐藏整个页面；
- 请求取消和页面卸载必须正确处理；
- Query key 必须包含隐藏 workspace ID 或 instance scope。

---

## 18. Multica 参考边界

### 18.1 可以参考

- Dashboard 三层 surface 体系；
- Sidebar 信息密度；
- 48px PageHeader；
- Collection page 结构；
- Empty / Error / Loading 语法；
- Issues 多视图组织方式；
- Runtime 图表结构；
- Chat thread + timeline 结构；
- 响应式 Sidebar 和 detail panel 思路；
- selected / hover / active 状态纪律。

### 18.2 不直接复制

- `apps/web` 组件源码；
- `packages/ui` 组件源码；
- `packages/views` 页面源码；
- Multica logo、图标和品牌资源；
- 页面文案；
- 营销插画；
- 具有明显识别性的整体页面组合。

### 18.3 不参考的产品机制

- Auth；
- Onboarding account flow；
- Workspace switcher；
- Invitations；
- Members；
- Permissions；
- Billing；
- Seat；
- Public/private visibility；
- 跨 Workspace unread。

### 18.4 许可要求

Multica License 将 `apps/web`、`apps/desktop`、`apps/mobile`、`packages/views` 和 `packages/ui` 派生界面纳入其 UI 定义，并限制移除或修改 Multica 品牌。实施时必须坚持独立设计和独立代码；若未来决定直接复用，需要先完成法律审查并取得必要授权或品牌豁免。

---

## 19. 性能要求

- 中等数据规模下首个可交互页面小于 2 秒；
- Shell 首屏不等待全部业务 Query；
- route loading 只显示目标页面 skeleton；
- 1,000 个 Issues 使用虚拟化或分页；
- 10,000 条 Session events 不得一次性全部挂载；
- 长 transcript 使用虚拟列表或渐进加载；
- 图表数据在 core 层预聚合；
- 后台刷新不得导致页面整体 reflow；
- Sidebar 和 PageHeader 切换不产生布局抖动；
- realtime 高频事件按帧或批次合并；
- 不因隐藏 detail panel 加载重量级编辑器；
- rich editor、图表和 transcript viewer 可动态加载。

### 19.1 建议基线数据集

- 1,000 Issues；
- 100 active/recent Sessions；
- 单 Session 10,000 events；
- 50 Agents；
- 20 Runtimes；
- 500 Skills；
- 100 Inbox items。

---

## 20. 测试与质量要求

### 20.1 单元测试

- 状态到视觉映射；
- route icon map；
- duration/token/cost formatter；
- filter/sort；
- Execution Spine event grouping；
- mode fallback；
- realtime reducer/patch；
- query key；
- responsive helper；
- sensitive value redaction。

### 20.2 组件测试

- PageHeader；
- Sidebar selected + hover；
- Dialog focus；
- Inbox 三类卡片；
- Board keyboard move；
- Session controls；
- pending/retry message；
- error and empty states；
- unknown enum fallback；
- theme switching；
- i18n overflow。

### 20.3 Playwright 关键路径

1. 根路径进入 Overview，无登录跳转；
2. 创建 Issue；
3. List/Board 切换；
4. 拖拽改变状态并收到 realtime 校准；
5. 打开 Issue Detail；
6. 启动 Session；
7. Approve / Deny；
8. Pause / Resume / Stop；
9. 处理 Clarification；
10. 从 Inbox 跳到 Session；
11. Runtime offline / reconnect；
12. Search 打开实体；
13. 手机 Sidebar 和 detail 导航；
14. light / dark 截图；
15. zh-CN / en / ja 截图。

### 20.4 视觉回归尺寸

- 375×812；
- 768×1024；
- 1280×800；
- 1440×900；
- 1920×1080。

### 20.5 人工 QA

- 200% 浏览器缩放；
- keyboard-only；
- reduced motion；
- 长中文标题；
- 长英文无空格内容；
- CJK 与 code 混排；
- 离线；
- 低速网络；
- 高频 realtime；
- stale cache；
- Runtime 在 destructive 操作中断连。

---

## 21. 验收标准

### 21.1 单用户验收

- 应用不存在登录入口；
- 根路径不跳转 `/login`；
- 浏览器不存用户 token；
- URL 不出现 workspace slug；
- 导航中不存在 Members、Invitations、Billing、Workspace switcher；
- 评论、审批和创建不要求 member ID；
- daemon/runtime credential 保持有效；
- 非 loopback 部署有明确安全提示。

### 21.2 视觉验收

- 所有主页面使用同一 Sidebar 和 PageHeader；
- 页面标题左边缘一致；
- light/dark 均满足对比度；
- 页面不依赖硬编码颜色；
- loading、empty、error 使用统一组件；
- selected 在 hover 时仍明确；
- 主要页面在五个目标尺寸无不可达操作；
- Execution Spine 在所有五种模式中使用一致语法；
- 不呈现 Multica logo、品牌名或直接复制页面。

### 21.3 功能验收

- Overview 汇总真实数据；
- Inbox 三类事项可闭环处理；
- Issues 拖拽可回滚；
- Sessions 有一级列表；
- Session 控制等待 authoritative state；
- tool call/result 可展开并安全截断；
- realtime reconnect 后数据一致；
- 搜索覆盖全部核心实体；
- Activity 可追溯 mutation；
- Usage 缺价格时不伪造成本。

### 21.4 工程验收

- TypeScript strict 无错误；
- ESLint 无 error；
- 单元和组件测试通过；
- Playwright 关键路径通过；
- 没有新增服务端实体 Zustand store；
- `packages/ui` 不导入 core；
- `packages/views` 不导入 Next.js API；
- API 响应有 schema；
- 新状态均有 unknown fallback；
- 无真实 Agent CLI 被默认测试调用。

---

## 22. 分阶段实施计划

### Phase 0：契约与路由整理

目标：先建立正确的单用户基础，避免在旧 workspace/auth 模型上建设新 UI。

- 增加 instance/bootstrap contract；
- 统一 workspace UUID；
- 扁平化路由；
- 删除登录与 AuthGate；
- 删除 Members 页面和导航；
- 移除浏览器 author/member 输入；
- 保留 daemon/runtime token；
- 添加安全部署说明。

验收：无需登录即可从 `/` 使用 Issues 和 Sessions，所有 Query 使用同一隐藏 workspace ID。

### Phase 1：Design System

- Tailwind 4；
- semantic tokens；
- 字体；
- UI primitives；
- light/dark；
- PageState；
- StatusBadge；
- Story/demo page 或组件测试矩阵。

验收：所有 primitive 的状态、键盘和主题表现通过。

### Phase 2：Dashboard Shell

- Sidebar；
- PageHeader；
- Breadcrumb；
- ConnectionStatus；
- Search/Command；
- Responsive Sheet；
- 全局新建 Issue；
- route loading/error boundary。

验收：所有现有页面进入统一 Shell，导航完整可达。

### Phase 3：核心工作流

- Overview；
- Inbox；
- Issues List/Board；
- Issue Detail；
- Sessions List；
- Session Detail；
- Execution Spine。

验收：创建任务、启动执行、处理阻塞、控制 Session、查看证据完整闭环。

### Phase 4：资源与配置

- Agents；
- Squads；
- Autopilots；
- Projects；
- Runtimes；
- Skills。

验收：所有核心执行资源可查看和配置，不出现成员权限语义。

### Phase 5：观察与质量

- Usage；
- Activity；
- 图表；
- 性能优化；
- 全面 i18n；
- 无障碍；
- 视觉回归；
- 文档同步。

验收：达到本文第 21 节全部标准。

---

## 23. 推荐拆分的实现 PR

1. `refactor(web): replace auth workspace routing with instance bootstrap`
2. `feat(ui): add semantic tokens and core primitives`
3. `feat(web): add dashboard shell and responsive navigation`
4. `feat(web): add overview and global command palette`
5. `feat(issues): redesign list board and detail surfaces`
6. `feat(sessions): add sessions index and execution spine`
7. `feat(inbox-chat): redesign attention and conversation flows`
8. `feat(resources): redesign agents squads runtimes and skills`
9. `feat(observability): redesign usage and activity`
10. `test(web): add responsive visual and critical-flow coverage`

每个 PR 应做到：

- 不保留永久双轨实现；
- 包含对应测试；
- 包含 light/dark 截图核验；
- 不修改无关后端协议；
- 更新受影响文档。

---

## 24. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 直接移植 Multica UI | 许可和品牌风险 | 独立设计、独立实现、保留设计决策记录 |
| 一次性引入完整组件栈 | Bundle 和维护成本增加 | 按页面需求逐批引入 primitives |
| 删除登录后暴露公网 | 可远程控制 Agent | loopback 默认、反向代理/VPN、明确警告 |
| workspace 仍在数据层 | 前端概念泄漏 | InstanceProvider 统一隐藏 |
| slug/UUID 混用 | Query 失败和缓存分裂 | Phase 0 强制统一 workspace ID |
| realtime 与 optimistic update 冲突 | UI 回跳或重复 | 单 Query cache、自事件去重、authoritative settle |
| Session event 量过大 | 页面卡顿 | 虚拟化、分页、事件合并 |
| 视觉过度接近普通后台 | 产品缺少辨识度 | 将 Execution Spine 作为唯一视觉签名 |
| 视觉为了“AI”过度动效 | 分散注意力 | 动效只用于状态变化，支持 reduced motion |
| i18n 后补 | 布局返工 | 从 primitives 和 PageHeader 阶段同步验证 CJK |

---

## 25. Definition of Done

本设计需求完成实施的定义：

1. 产品以单用户、无登录方式打开；
2. Workspace、Member、Role、Invitation、Billing 不出现在产品表面；
3. 所有核心路由使用统一 Dashboard Shell；
4. Overview 可以准确展示系统健康与待处理事项；
5. Issues、Inbox、Sessions 形成完整操作闭环；
6. 五种编排模式在 Execution Spine 中表现一致且各具结构；
7. Agent、Runtime、Skill 与 Autopilot 页面达到可日常使用的完成度；
8. light/dark、三语言、移动端、键盘和 reduced motion 均通过验收；
9. 大数据量下页面满足性能要求；
10. 实现未复制 Multica UI 源码或品牌资产；
11. 默认测试不调用真实 Agent CLI；
12. 相关产品、API、安全和部署文档同步完成。

---

## 26. 设计决策记录

| ID | 决策 | 状态 |
| --- | --- | --- |
| WEB-D1 | Web-only，不建设 Desktop/Mobile | Accepted |
| WEB-D2 | 无浏览器登录 | Implemented |
| WEB-D3 | 单用户，隐藏 default workspace | Implemented |
| WEB-D4 | URL 扁平化，不含 workspace slug | Implemented |
| WEB-D5 | 保留 workspace 数据字段和隔离 | Implemented |
| WEB-D6 | 保留 daemon/runtime credentials | Accepted |
| WEB-D7 | Multica 仅作设计参考，不直接派生 UI 代码 | Accepted |
| WEB-D8 | Execution Ledger 为视觉方向 | Implemented |
| WEB-D9 | Execution Spine 为唯一强调型视觉签名 | Implemented |
| WEB-D10 | Dark 为默认主题，同时完整支持 Light | Implemented |
| WEB-D11 | Sessions 升为一级导航 | Implemented |
| WEB-D12 | 面向用户将 Audit 命名为 Activity | Implemented |

`Implemented` 表示该决策已由当前 Web 重构落地；后续如需偏离，必须补充新的 ADR 并迁移现有界面与契约。
