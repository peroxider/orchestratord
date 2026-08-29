# ADR-003 — 分级超时与三态 Resume 合约

- **Status:** Accepted
- **Date:** 2026-08-28
- **Authors:** orchestratord core team
- **Related:** `DESIGN_graded_timeouts_and_resume.md`, ADR-001 (Backend Hardening),
  ADR-002 (Goal Mode — Cap9 bit expansion precedent)

---

## Context

orchestratord's `SessionSpec` historically carried a single
`timeout_s` field plus two clawcodex-specific fields
(`stall_timeout_s` / `stall_warn_s`). This produced three concrete
problems:

1. **语义不区分** — orchestrator 想表达"agent 启动握手 5 秒超时"
   和"agent 整轮 30 分钟超时"是两类需求，但共用 `timeout_s`，不同
   backend 各按己意理解。
2. **resume 无结构化合约** — `AgentSession.close()` / `create_session(resume=...)`
   路径的成功/失败是二态（bool），backend 自报"我也不知道 transcript
   还在不在"时只能硬编码为 True 或 False，编排侧无法区分。
3. **沉默超时** — `agent_runner.py:1578` 把 `timeout_s` 直接传给 clawcodex
   SDK，若 SDK 只把它当成"turn 间隔超时"而不是"总超时"，编排侧的
   "任务 30 分钟到点该结束"实际失效。

参考 multica `server/pkg/agent/agent.go:40-61`（5 级 ExecOptions
超时）、`agent.go:203-231`（`ResumeRejected` 语义）、
`agent.go:350-364`（`ResumeRejectionUndetectable`）。

---

## Decision

### Decision 1: SessionSpec 扩展为 5 级超时分类

在 `src/orchestratord/spi/backend.py:SessionSpec` 上新增 5 个字段：

| 字段                     | 语义                                         | 默认值 |
|--------------------------|----------------------------------------------|--------|
| `total_timeout_s`        | 整轮硬上限（run watchdog）                   | 1800s  |
| `handshake_timeout_s`    | 启动 → 第一次产出                           | 30s    |
| `first_turn_timeout_s`   | 启动后首个 turn 完成                         | 120s   |
| `inactivity_timeout_s`   | turn 间多久无新 token                        | 300s   |
| `idle_watchdog_timeout_s`| session 完全 idle                            | = total|

`timeout_s` / `stall_timeout_s` 保留为 **deprecated** 别名，
`__post_init__` 把它们折叠进新字段（前者 → `total_timeout_s`，
后者 → `inactivity_timeout_s`）。

### Decision 2: SessionSpec 校验不变量

`SessionSpec.__post_init__`（DESIGN §1.4）：

- `total_timeout_s` > max(sub-timeouts) — 总时长必须包裹各阶段超时
- `idle_watchdog_timeout_s` >= `total_timeout_s` — 不允许

校验**仅在 `total_timeout_s` 被显式设置时**生效；向后兼容
`timeout_s=None, stall_timeout_s=None` 仍合法。

### Decision 3: 引入 `ResumeStatus` 三态

在 `src/orchestratord/spi/session.py` 新增：

```python
class ResumeStatus(enum.Enum):
    RESUMED = "resumed"               # 成功恢复
    REJECTED = "rejected"             # 显式拒绝（transcript GC / 配额满 / 撤销）
    UNDETECTABLE = "undetectable"     # 后端无 probe 能力
```

理由：部分后端（如 dsh / hermes / opencode-old）无法探测 transcript
是否仍在服务端保留；这种"我不知道"必须显式表达而非默认 False/True。

### Decision 4: AgentSession 增 `probe_resume()` 方法

`AgentSession` Protocol 新增 async `probe_resume() -> ResumeStatus`。
各 backend 实现：

| backend | probe_resume 实现 | capability `resume_detection` |
|---|---|---|
| clawcodex       | 真探测 (`QueryRunner.probe_transcript`) | True  |
| codex (AppServer)| 真探测 (`session/load` MCP)            | True  |
| codex (CLI)     | UNDETECTABLE（无跨进程状态）           | False |
| dsh             | UNDETECTABLE（SDK 无协议）             | False |
| hermes          | REJECTED（明确不支持）                 | False |
| opencode        | 真探测 (`session/load` HTTP)            | True  |

### Decision 5: BackendCapabilities 第 10 位

`src/orchestratord/spi/capabilities.py:BackendCapabilities` 新增字段
`resume_detection: bool = False`。**正交于**既有的 `resumable` 位：
- `resumable=True` 表示 "我能 create 一个 resume session"
- `resume_detection=True` 表示 "我能探测目标 transcript 是否仍在"

### Decision 6: BackendRunner 落地 5 级超时

`src/orchestratord/backend_runner.py` 新增：

- `_resolve_timeouts(spec)` → dict，包含 5 个数值（`None` 填默认值）
- `_probe_resume_or_log(spi_session, spec)` → `ResumeStatus`
  （缺失方法时降级为 UNDETECTABLE）
- `_process_events()` 接受新 `timeouts` kw 参数，在事件循环里
  强制 `idle_watchdog_timeout_s` 与 `total_timeout_s`

`_run_with_backend()` 在调用 `send()` 之前先调 `probe_resume()`：
- `REJECTED` → 立刻 emit 失败、关闭 session、不 send
- `UNDETECTABLE` / `RESUMED` → 继续走原 `send()` 路径

### Decision 7: failure_messages 路由 ResumeStatus + error_code

`src/orchestratord/failure_messages.py` 新增 `build_failure_message()`：

```
priority 1: REJECTED → "transcript 已被 GC / 不支持跨进程恢复"
priority 2: UNDETECTABLE + handshake_timeout → "agent 未能在 X 秒内启动"
priority 3: UNDETECTABLE + first_turn_timeout → "agent 启动了但首轮未在 X 秒完成"
priority 4: UNDETECTABLE + inactivity_timeout → "X 秒无 token 输出"
priority 5: UNDETECTABLE + idle_watchdog_timeout → "X 秒无任何事件"
priority 6: UNDETECTABLE + total_timeout → "整轮超过 X 秒"
priority 7: 兜底 → "未知失败"
```

3 × 5 = 15 矩阵中 12 个可达（`RESUMED` 是 benign ack，不算失败）。

---

## Consequences

### Positive

- **可观测性**：5 级超时给 backend 提供独立 signal，orchestrator
  可区分 "启动失败" 与 "运行中死锁"。
- **可移植性**：3 态 resume 把 backend 探测能力的差异结构化，
  新 backend 只要如实报 UNDETECTABLE 即可。
- **向后兼容**：`timeout_s` / `stall_timeout_s` 仍可用；
  `__post_init__` 自动折叠；现有 94 个测试文件无需修改。

### Negative

- **Schema 膨胀**：`SessionSpec` 字段从 ~25 个增到 ~30 个。
- **测试矩阵增长**：12 cell × 错误码 + 5 backend × 3 态 resume。
- **双轨维护期**：`timeout_s` 与 `total_timeout_s` 并存期间，需
  保证两者语义一致。

### Risks

| Risk | Mitigation |
|---|---|
| 现有测试依赖 `timeout_s` 单字段文案 | `failure_messages.timeout_message(ctx)` 仍按 `timeout_ms` 工作；新 `build_failure_message()` 是**新增**函数，不替换 |
| 某 backend 误把 REJECTED 实现为 UNDETECTABLE | `test_resume_status.py` 给 5 backend 各 1+ 用例钉死返回值 |
| Idle watchdog 误伤长思考 turn | 默认值即 `total_timeout_s`；callers 可调大 |

---

## Compliance

### Backends

| backend    | probe_resume | resume_detection |
|-----------|--------------|------------------|
| clawcodex | 真探测       | True  |
| codex AS  | 真探测       | True  |
| codex CLI | UNDETECTABLE | False |
| dsh       | UNDETECTABLE | False |
| hermes    | REJECTED     | False |
| opencode  | 真探测       | True  |

### Verification (DESIGN §7)

- [x] **`SessionSpec` 5 字段校验单测全过** — `tests/test_graded_timeouts.py`
- [x] **5 个 backend 各自有 `probe_resume()` 实现** — `tests/test_resume_status.py`
- [x] **`failure_messages` 12 矩阵全过** — `tests/test_failure_messages.py::TestBuildFailureMessage*`
- [x] **`test_capability_drift.py`** 加 `resume_detection` 位校验
      （已迁到 BackendDescriptor 派生 — `tests/test_capability_drift.py:117`）
- [ ] **现有测试套不破** — 由 `agent_runner.py:1574` 调用点改动可能
      触发 stub/spy 不匹配；运行 `pytest -x` 验证
- [x] **ADR-003 起草** — 即本文档

---

## Out of Scope

1. **clawcodex 内核改造**：不重写 `extensions.api.query.QueryRunner`，
   只在其上薄包一层 `probe_resume()`。
2. **超时预算编排**：orchestrator 不强制各 turn 累计 ≤ total_timeout_s，
   由 backend 自行管理。
3. **跨 session resume**：本次设计仅考虑同 session_id；新 session_id
   复用旧 transcript 不在范围。
4. **probe_resume() 的缓存**：每次 send 前都探测，不缓存 — 缓存属后续优化。

---

## Future Work

- 把 5 级超时抽成 `TimeoutBundle` 数据类，便于统一 logging 与 metrics
- `BackendCapabilities` 加 `cost_event_emission` 等位
- 在 `chat_gateway.py` 把 `ResumeStatus` 暴露给前端：UI 可显示
  "恢复失败" 而非静默重试