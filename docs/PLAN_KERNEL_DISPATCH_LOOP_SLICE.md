# 收尾计划：Kernel dispatch-loop 切片（编排解耦第二阶段）

| 项目 | 内容 |
| --- | --- |
| 状态 | Draft（待评审） |
| 日期 | 2026-09-09 |
| 前置文档 | `DESIGN_ORCHESTRATION_BUSINESS_DECOUPLING.md`（其 P0–P6 已全标 ✅，但核心目标 G1/G2 只完成一半，见该文档 P6「偏差（记录）」） |
| 范围 | `src/orchestratord/orchestrator.py` 的机制/业务最终分离、`kernel/kernel.py` 主循环、`applications/issue_pr/` 业务补全 |
| 定位 | 承接设计文档中反复注记的「随 Kernel dispatch-loop 切片收敛」——**本文是该切片的执行计划** |

---

## 0. 摘要

设计文档的 P0–P6 完成了**解耦的前置工程**（协议定义、结果态分离、Prompt/配置分段、注册表、session 去业务化、monkey-patch 拆除、EchoApplication 演练）。但 P4 的 C1/C2a/C2b/C2c 四切片采用的是 **3-seam 委托（delegation）而非抽取（extraction）**——业务函数体搬进了 `applications/issue_pr/lifecycle.py`（821 行）与 `provider.py`（410 行），却留下：

1. **19 个 `_host` 回调方法**仍躺在 Orchestrator（`_IssueDispatchHost`/`_IssueLifecycleHost` 协议成员）；
2. **6 条 poll 副循环**（review feedback / rebase conflict / PR conflict scan / escalation / clarification）从未迁出 `_poll_and_dispatch`；
3. **`_run_issue`（690 行）中段**仍含 issue→AgentTask 映射、复现门、cannot_proceed、空分支检测等业务逻辑；
4. **`Outcome` 归位但无消费者**——`_run_issue:3581` 调 `interpret()` 后丢弃返回值。

净效果：`orchestrator.py` 仍是 4632 行上帝类（767 处 `issue` 引用），`applications/issue_pr/` 装的是"半套业务"。本文把这些残留分为三类（A 委托壳 / B 业务残留 / C 机制键残留），并给出 5 阶段收尾路径。

> **行号声明**：本文所有 `file:line` 引用以计划撰写时工作树为准。因 peer 会话并行推进 master（设计文档已记录 905 行分叉），实施每阶段前须用 `grep -n` 复核行号。

---

## 1. 残留分类总览

判据与收尾动作：

| 类别 | 判据 | 收尾动作 |
| --- | --- | --- |
| A. 委托壳残留 | 方法体只剩 `app.prepare_launch()` / `_work_provider.poll()` 转发 | Kernel 主循环建好后**自然消失**，无需单独重构 |
| B. 业务残留 | 业务逻辑体仍在 Orchestrator，经 `self._host` 回调或被 poll 循环直呼 | **迁入 `applications/issue_pr/`**，收尾主战场 |
| C. 机制键残留 | 纯机制逻辑，但以 `issue_id` 而非 `dedup_key`/`run_id` 为键 | **改键 + 去业务字面**，与 Kernel 抽取同步 |

### 1.1 A 类：委托壳残留（随抽取消失）

| 方法 | 说明 |
| --- | --- |
| `_launch_issue` `orchestrator.py:2486` | 纯机制壳：`prepare_launch` → 建 `AgentSession` → `decorate_session` → viz journal → `post_viz_gate` → running map/task |
| `_run_issue` `:2898` 终态段 | 已迁 `interpret()`，只剩 `:3581` 一处调用 |
| `_poll_and_dispatch` `:1024` 末行 | `await self._work_provider.poll()`（`:1053`），业务链本体在 `provider._dispatch_candidates` |

### 1.2 B 类：业务残留（收尾主战场）

**B1 — 19 个 `_host` 回调方法（协议成员，实现仍在 Orchestrator）**

| 族 | 方法（`orchestrator.py` 行号） |
| --- | --- |
| 意图解析 | `_resolve_intent`:1080、`_resolve_command_intent`:1171、`_post_command_acknowledgement`:1197、`_is_command_author_eligible`:1232、`_reject_unauthorized_command`:1274、`_check_retry_rate_limit`:1324、`_post_retry_rejection`:1385、`_check_rebase_rate_limit`:1534 |
| 依赖/准备 | `_dependencies_satisfied`:1061、`_prepare_intent_reset`:1467、`_prepare_intent_session`:2191、`_sync_tracker_issue_state`:2575 |
| rebase | `_process_rebase_intent`:1574、`_launch_rebase_resolution`:1829、`_finalize_rebase_resolution`:1930、`_rebase_conflict_resolved`:2016、`_prepare_rebase_session`:2115 |
| review-feedback | `_uses_review_feedback_followup`:2267、`_launch_followup_with_pending_reviews`:2344、`_launch_review_followup`:2414、`_complete_read_only_chat_followup`:2275 |
| summary/finalize | `_update_issue_summary`:3588、`_apply_review_rules`:3647、`_reply_to_processed_feedback`:3658、`_post_feedback_summary`:3699 |
| 控制命令 handler | `_handle_rebase_control`:2128、`_handle_review_followup_control`:3745、`_handle_review_retry_control`:4521、`_handle_review_approve_control`:4527、`_handle_retry_control`:4452、`_handle_followup_control`:4463 |

> `lifecycle.py:776` 的 `control_commands()` 返回的 6 个 handler 是 `await host._handle_xxx_control(...)` 闭包——**命令注册表迁了，命令实现没迁**。

**B2 — `_poll_and_dispatch`:1024 里的 6 条业务副循环（从未动）**

```
_process_retry_queue()                        # 机制但 issue_id 键（归 C）
_process_escalated_issues()       :3969       # 业务：澄清耗尽升级
_process_review_feedback()        :2306       # 业务：检视反馈轮询
_process_pending_rebase_conflicts():1667       # 业务：rebase 冲突
_process_pr_conflict_scan()       :1768       # 业务：PR 冲突扫描
_clarification_resolver.poll_clarification_answers()  # 业务：澄清通道
```

**B3 — `_run_issue`:2898 中段未迁的执行期业务（约 400 行）**

- `issue_to_agent_task(session.issue, ...)`:2912 — issue→AgentTask 映射（`issue_registry/task_mapping.py`）
- `_repro_gate_applies` / `_run_repro_gate`:2717/:2733 — 复现门
- cannot_proceed / premise_not_met 处理（`read_cannot_proceed` / `format_cannot_proceed_comment`）:2998–3026
- 空分支检测（`get_repo_root` / `get_file_status` / `_run_git` rev-parse）:3041–3057

### 1.3 C 类：真正机制残留（改键去字面）

| 方法（`orchestrator.py`） | 问题 |
| --- | --- |
| `_process_retry_queue`:4083、`_recover_pending_retries`:873、`_retry_requeue_limit`:4042、`_requeue_retry`:4050 | `RetryItem.issue_id` 键，应改 `dedup_key` |
| `_recover_stale_running_records`:862、`_recover_persistent_states`:923 | 直读业务枚举 `IssueStatus` 与 `record.issue_id` |
| `_schedule_retry`:3816 | 混合体：retry 数学已迁 `kernel/dispatch.py`，入口仍按 `session.issue.id` 记 `next_retry_at` |
| `_issue_tasks` dict、`get_event_stream(issue_id)`:4618 | 任务/事件流映射，键是 `issue_id` |
| `_emit_im_event(issue_id, ...)`:563、`_issue_payload`:628、`_session_payload`:649 | 事件总线第一参数是 `issue_id`；`_issue_payload`/`_session_payload` 直接构造 issue 事件 schema |
| `_update_run_diagnostics`:2590 | 诊断回调读 `session.issue.id` |

---

## 2. 完成定义（Definition of Done）

收尾全部完成时，以下断言须为真：

1. `kernel/kernel.py` 存在，`OrchestrationKernel` 实现 §5 主循环；`orchestrator.py` 不再含轮询/并发/生命周期主循环。
2. `_poll_and_dispatch` 的 6 条业务副循环全部经 `on_kernel_event(POLL_TICK)` 由 `IssueToPrLifecycle` 订阅执行。
3. 19 个 `_host` 回调方法迁入 `applications/issue_pr/`；两个 `_host` 协议收缩到只含机制回调。
4. `_run_issue` 收缩为纯机制（semaphore → runner → interpret → finally）。
5. `Outcome` 被 Kernel 消费（`_apply_outcome`：RETRY 定时 / SPAWN 入队 / DISPOSE / WAIT_EXTERNAL 挂起）。
6. 协议升级为全签名：`prepare_run(item, ctx)` / `interpret_result(item, result, ctx)`；`RunContext` 物化到 `AgentSession`。
7. 机制域以 `dedup_key`/`run_id` 为键，`kernel/` 与（作为组合根的）`orchestrator.py` 零 import `issue_registry`/`tracker`/`repro_gate`/`review_feedback`/`premise_check`。
8. `orchestrator.py` 退化为组合别名（或删除，改 5 处 import 点）。

---

## 3. 分阶段计划

每阶段行为等价、可独立 merge、先立护栏后动刀（沿用设计文档 P0 原则）。

### 阶段 1 — 建 `kernel/kernel.py` 主循环

**目标**：`OrchestrationKernel` 承载轮询/并发/生命周期主循环；`Orchestrator` 降为组合根。

**具体文件**：
- 新建 `src/orchestratord/kernel/kernel.py` — `OrchestrationKernel`（`run()` + `_dispatch_loop()` + `_execute()` + `_apply_outcome()`）。
- 新建 `src/orchestratord/kernel/control.py` — 机制控制命令（pause/resume/stop/takeover/gateway），对应设计 §6 目录。
- 改 `src/orchestratord/kernel/events.py` — `KernelEventKind` 增 `POLL_TICK`。
- 改 `src/orchestratord/orchestrator.py` — 迁出 `run()`:744 的轮询壳、`_semaphore`、`_tasks`、`_issue_tasks`（改键 `dedup_key`）。

**具体方法（迁入 Kernel）**：
- `run`:744 主循环（`while not shutdown` / `_poll_and_dispatch` / 心跳 task / `_cancel_all_tasks`:4625）
- `_poll_and_dispatch`:1024 的机制段（`on_poll_start` / `poll_check_in_progress` / `_refresh_dynamic_title_prefix_filter`:981 / `finally` 段）
- 并发治理：`_semaphore`、`_tasks`、`_issue_tasks`（→ 按 `dedup_key` 命名空间）

**验收**：
- `OrchestrationKernel` 可独立实例化，`run()` 与 EchoApplication 走 Kernel 主循环跑通（`tests/test_echo_application.py` 全绿）。
- 架构护栏：`kernel/kernel.py` 零 import `applications`/`issue_registry`/`tracker`。
- `Orchestrator` 不再有 `while not self._shutdown_event.is_set()` 主循环（grep 断言）。

### 阶段 2 — `on_kernel_event` 扩展点 + 6 条副循环迁移

**目标**：poll 循环不再直呼业务副循环。

**具体文件**：
- 改 `src/orchestratord/applications/issue_pr/lifecycle.py` — 实现 `on_kernel_event(event)`（当前空壳），订阅 `POLL_TICK` 执行 5 条业务副循环。
- 改 `src/orchestratord/kernel/events.py` — 确认 `KernelEvent.payload` 可携带 `poll_tick` 上下文。
- 改 `src/orchestratord/orchestrator.py` — `_poll_and_dispatch` 删除 5 条业务直呼，改为 `Kernel` 广播 `POLL_TICK`。

**具体方法（迁入 application）**：
- `_process_escalated_issues`:3969
- `_process_review_feedback`:2306
- `_process_pending_rebase_conflicts`:1667
- `_process_pr_conflict_scan`:1768
- `_clarification_resolver.poll_clarification_answers()`（澄清通道，迁入 `on_kernel_event` 内）
- 留 Kernel：`_process_retry_queue`:4083（改键见阶段 5）

**验收**：
- `_poll_and_dispatch` 源码零 `_process_escalated_issues` / `_process_review_feedback` / `_process_pending_rebase_conflicts` / `_process_pr_conflict_scan` / `poll_clarification_answers` 直呼（AST 断言）。
- 行为等价：`tests/test_orchestrator_daemon.py`（31）全绿，副循环调用顺序与原 poll 循环一致（快照）。

### 阶段 3 — 消解 19 个 `_host` 回调

**目标**：业务方法迁入 `applications/issue_pr/`；`_host` 协议收缩到机制回调。

**具体文件**（对应设计 §6 目录）：
- 新建 `src/orchestratord/applications/issue_pr/interpret.py` — 意图解析族 + rebase 族 + review-feedback 族 + summary/finalize 族（19 个方法中的非命令部分）。
- 新建 `src/orchestratord/applications/issue_pr/commands.py` — 6 个控制命令 handler 实现（`_handle_rebase_control` 等）。
- 改 `src/orchestratord/applications/issue_pr/lifecycle.py` — `control_commands()` 的 6 个适配闭包从 `host._handle_xxx_control` 改为调用 `commands.py` 内实现。
- 改 `src/orchestratord/applications/issue_pr/provider.py` — 收缩 `_IssueDispatchHost` 协议。
- 改 `src/orchestratord/orchestrator.py` — 删除 19 个迁出的方法。

**具体方法（迁出，file:line 见 1.2 B1 表）**：
- 意图解析族 8 个、依赖/准备族 4 个、rebase 族 5 个、review-feedback 族 4 个、summary/finalize 族 4 个、控制命令 handler 6 个（合计 31 个，重叠的 `_sync_tracker_issue_state` 归入状态同步族）。

**验收**：
- `_IssueDispatchHost`（provider.py:38）与 `_IssueLifecycleHost`（lifecycle.py:64）成员从 14+16 收缩到只剩机制回调（`_emit_im_event` 改造后、`_schedule_retry` 机制入口、`_session_payload` 若保留则下沉）。
- 全库 grep：`_handle_rebase_control` / `_resolve_intent` / `_process_review_feedback` 等仅存于 `applications/issue_pr/`，`orchestrator.py` 无定义。
- 行为等价：`tests/test_im_events.py`（67）+ `test_orchestrator_daemon.py`（31）全绿。

### 阶段 4 — `_run_issue` 中段收敛 + 协议全签名

**目标**：`_run_issue` 收缩为纯机制；协议升级全签名；`Outcome` 被消费。

**具体文件**：
- 改 `src/orchestratord/applications/issue_pr/lifecycle.py` — 升级 `prepare_launch(issue)` → `prepare_run(item, ctx)`；`interpret(session)` → `interpret_result(item, result, ctx)`。
- 改 `src/orchestratord/session_state.py` — `AgentSession` 增 `run_context` 字段，物化 `RunContext`。
- 改 `src/orchestratord/kernel/kernel.py` — `_execute()` 调 `prepare_run` / `interpret_result` / `_apply_outcome`。
- 改 `src/orchestratord/orchestrator.py` — `_run_issue` 中段业务迁出，收缩为 `semaphore → runner.run → interpret → finally(workspace cleanup + claimed 释放)`。

**具体方法（迁出 `_run_issue` 中段）**：
- `issue_to_agent_task(...)`:2912 调用 → 迁 `prepare_run`
- `_repro_gate_applies`:2717 / `_run_repro_gate`:2733 → 迁 `prepare_run` 前置阶段
- cannot_proceed 处理 :2998–3026 → 迁 `interpret_result`
- 空分支检测 :3041–3057 → 迁 `interpret_result`（或 `prepare_run` 后置）

**验收**：
- `_run_issue` 收缩到 < 150 行纯机制（AST 行数断言）。
- `Application` 协议全签名：`prepare_run(item, ctx)` / `interpret_result(item, result, ctx)` 被 `IssueToPrLifecycle` 实现，EchoApplication 协议闭环全绿。
- `_apply_outcome` 落地：`RETRY` → `_schedule_retry`；`SPAWN` → 入队衍生 `WorkItem`；`DISPOSE` → 终结；`WAIT_EXTERNAL` → 挂起。
- `_run_issue` 源码零 `issue_to_agent_task` / `read_cannot_proceed` / `get_file_status` 直呼。

### 阶段 5 — 机制键去业务化 + 别名收尾

**目标**：机制域以 `dedup_key`/`run_id` 为键；`orchestrator.py` 退化为组合别名或删除。

**具体文件**：
- 改 `src/orchestratord/orchestrator.py`（或 Kernel）— 键与事件去业务化。
- 改 `src/orchestratord/session_state.py` / `kernel/dispatch.py` — `RetryItem.issue_id` → `dedup_key`。
- 改 `src/orchestratord/applications/issue_pr/` — 承接 `_issue_payload` / `_session_payload` 事件 schema。
- 改 `src/orchestratord/orchestration_subsystem.py` / 5 处 import 点 — 若删 `orchestrator.py`。

**具体方法（改键/下沉）**：
- `_issue_tasks` dict、`get_event_stream(issue_id)`:4618 → `dedup_key` / `run_id`
- `_emit_im_event(issue_id, ...)`:563 → 首参 `run_id`/`dedup_key`
- `_issue_payload`:628 / `_session_payload`:649 → 下沉 application（Kernel 发通用 `SessionEvent`，IM 载荷由 application 装饰）
- `_recover_stale_running_records`:862 / `_recover_persistent_states`:923 → 去掉 `IssueStatus` 直读，改为 application 提供状态谓词回调
- `_schedule_retry`:3816 → 按 `dedup_key` 记 `next_retry_at`

**验收**：
- 架构护栏新增断言：`kernel/` 与组合根零 import `issue_registry`/`tracker`/`repro_gate`/`review_feedback`/`premise_check`。
- `RetryItem` / `WorkItem` / `KernelEvent` 全程 `dedup_key`，全库 grep 无机制路径的 `issue_id` 键。
- `orchestrator.py` 删除或退化为 `Orchestrator = Kernel + IssueToPrApplication` 预装配别名；`tests/test_im_events.py` 的 monkeypatch seam 相应调整。
- 全量回归 2 基线失败外全绿（基线：`test_skills_cli::TestVerify`、`test_skills_loader::TestBuiltinDiscovery`）。

---

## 4. 验证闸门（每阶段通用）

1. **架构护栏**：`tests/test_architecture.py` 每阶段追加对应断言（阶段 1 加 kernel 零业务 import；阶段 5 加组合根零业务 import）。登记机器 `EXEMPTIONS`/`FIELD_READ_EXEMPTIONS` 已双清零，保持常设。
2. **移动保真 diff**：对 HEAD 做 strip 归一 + `self.`→`self._host.` 改写后逐行等值比对（沿用 C2b identity 探针法），证明"只搬不改"。
3. **行为等价回归**：`tests/test_im_events.py`（67）+ `tests/test_orchestrator_daemon.py`（31）+ `tests/test_echo_application.py`（7）+ 冒烟链路（issue→澄清→复现→执行→PR→检视跟进→rebase）。
4. **import 时序探针**：六种 import 顺序（orchestrator / lifecycle / provider / cli.main / cli.serve / cli.server）后 `issue_pr.app` 均不入 `sys.modules`、`app` 惰性 seam 完好。
5. **lint parity**：迁移前后规则多重集全等（moved-origin 项随块迁入，不修 lint 以保 move identity）。
6. **全量回归**：非基线失败零新增（基线失败逐一匹配）。

---

## 5. 风险与回滚

| 风险 | 等级 | 缓解 |
| --- | --- | --- |
| 主循环抽取改变事件时序（IM/心跳） | 高 | 阶段 1 单独成 PR；`test_im_events` 心跳时序快照锁序；`KernelHooks.on_kernel_start` 注入点与原 patch 时机对齐 |
| 6 条副循环迁 `on_kernel_event` 改变执行顺序 | 中 | `POLL_TICK` 在 dispatch 前/后两个时点广播，副循环顺序照搬原 `_poll_and_dispatch` 快照 |
| 19 个 `_host` 回调迁出导致 `import` 环 | 中 | 迁入模块顶层只 import `kernel`+低层；`interpret.py`/`commands.py` 不得 import `orchestrator`/`orchestration_subsystem` |
| `Outcome` 消费接入改变 retry 时序 | 中 | `_apply_outcome` 先做 DISPOSE/RETRY 直通等价，SPAWN/WAIT_EXTERNAL 留到全签名稳定后再启用 |
| `dedup_key` 改键破坏 DB/遥测兼容 | 低 | 键仅内存态改名；DB 字段（`issues` 表）不迁移（设计 §8 明文） |
| peer 会话并行推进 master 致行号漂移 | 中 | 每阶段开工前 `grep -n` 复核行号；用工作树字节快照而非 `git show HEAD:` 作 pre-image |

---

## 6. 术语对照

| 本文术语 | 设计文档出处 |
| --- | --- |
| 3-seam 委托 | §4.2 / P4 C2b「3-seam 保序委托」 |
| `_host` 回调 | `_IssueDispatchHost`（provider.py）/ `_IssueLifecycleHost`（lifecycle.py） |
| poll 副循环 | `_poll_and_dispatch` 中 poll 之外的周期业务循环 |
| dispatch-loop 切片 | 设计文档 P4/P6 反复注记的「后续阶段」 |
| `dedup_key` | §4.1 WorkItem 幂等键（issue→PR 用 issue_id） |
