# 分级超时与三态 Resume 合约 — 设计文档

> **状态：** 临时设计稿，待评审。
> **目标：** 把 `SessionSpec` 中单个 `timeout_s` 升级为 5 级超时分类，把 `resume` 的二态（成功 / 失败）升级为三态（恢复 / 拒绝 / 无法判定），让编排侧能在不同失败语义上分别处理。
> **参考：** multica `server/pkg/agent/agent.go:40-61`（5 级 ExecOptions 超时）、`agent.go:203-231`（`ResumeRejected` 语义）、`agent.go:350-364`（`ResumeRejectionUndetectable`）。
> **不解决：** 现有 `stall_timeout_s` / `stall_warn_s` 的移除（保留作为 deprecated 别名）。

---

## 0. 背景与现状

### 0.1 现状缺口

`SessionSpec`（`src/orchestratord/spi/backend.py:17-50`）当前只有 3 个时间字段：

- `timeout_s: float | None` — 总超时，传给 clawcodex `agent_runner.py:1578`
- `stall_timeout_s: float | None` — 静默超时，`agent_runner.py:1579`（仅 clawcodex 路径使用）
- `stall_warn_s: float | None` — 静默告警，仅 clawcodex

问题：
1. **语义不区分**：orchestrator 想表达"agent 启动握手 5 秒超时"和"agent 整轮 30 分钟超时"是两类需求，但当前共用 `timeout_s`，不同 backend 各自理解
2. **resume 无状态**：`backend_runner.py:286` 创建 session 时 `resume_session_id=None`，错误处理散在 `orchestrator.py:4763-4773`（`_handle_resume_control` 类方法）/ `failure_messages.py:41` — 没有结构化合约
3. **沉默超时**：`agent_runner.py:1578` 把 `timeout_s` 直接传给 clawcodex SDK；如果 clawcodex SDK 只把它当成"turn 间隔超时"而不是"总超时"，编排侧的"任务 30 分钟到点该结束了"实际失效
4. **后端失败模式不可观测**：`failure_messages.py:41-43` 写"任务执行超时（X 秒），Agent 未能在规定时间内完成修复"，但 SDK 实际是 5xx / 网络断开 / rate limit / transcript gone 哪种，没有结构化信号

### 0.2 multica 对应设计

| 概念 | multica 位置 | 字段 / 语义 |
|---|---|---|
| 总超时 | `agent.go:48` `Timeout` | 整轮硬上限 |
| 握手超时 | `agent.go:50` `HandshakeTimeout` | 启动 → 第一次产出 |
| 首轮超时 | `agent.go:51` `FirstTurnNoProgressTimeout` | 启动了但没出活 |
| 语义空闲超时 | `agent.go:49` `SemanticInactivityTimeout` | turn 间多久无新 token |
| 看门狗超时 | `agent.go:52` `IdleWatchdogTimeout` | session 完全 idle 的硬上限 |
| Resume 三态 | `agent.go:203-231` | `ResumeRejected`（拒绝） / `Clear`（可恢复，由 RESUMED 表达） |
| 后端无法判定 | `agent.go:350-364` `ResumeRejectionUndetectable` | "我不知道 transcript 还在不在" |

### 0.3 方案总览

| # | 子方案 | 范围 | 解决 |
|---|---|---|---|
| A | SessionSpec 加 4 个超时字段 + 1 个 deprecated 字段 | `spi/backend.py` + 5 个 backend 包 | 5 级超时分类 |
| B | 新增 `ResumeStatus` 枚举 + SessionResult 字段 | `spi/session.py` + `agent_runner.py` + `orchestrator.py:4763` | resume 三态结构化 |
| C | BackendRunner 把 timeout 分级落到 backend 适配 | `backend_runner.py` + 各 backend 包 | 让 backend 真正区分 |
| D | Failure messages 路由到 ResumeStatus | `failure_messages.py` + `orchestrator.py` | 编排侧失败响应可观测 |

---

## 1. 方案 A — SessionSpec 5 级超时

### 1.1 目标

让 orchestrator 能表达 5 种语义独立的超时，每个 backend 据此实现。保持向后兼容：`timeout_s` 仍存在，**deprecated** 为"总超时"的同义词。

### 1.2 新增字段

```python
# src/orchestratord/spi/backend.py — SessionSpec 增字段
@dataclass
class SessionSpec:
    cwd: str
    system_prompt: str | None = None
    model: str | None = None
    # ... 既有字段 ...
    timeout_s: float | None = None            # DEPRECATED: 等价 total_timeout_s
    stall_timeout_s: float | None = None     # DEPRECATED: 等价 inactivity_timeout_s
    stall_warn_s: float | None = None        # DEPRECATED: 保留供 clawcodex 旧路径

    # === 方案 A 新增：5 级超时分类 ===
    total_timeout_s: float | None = None        # 整轮硬上限（取代 timeout_s）
    handshake_timeout_s: float | None = None    # 启动到首次产出
    first_turn_timeout_s: float | None = None   # 启动后首个 turn 完成
    inactivity_timeout_s: float | None = None   # turn 间无 token 增量
    idle_watchdog_timeout_s: float | None = None  # session 完全 idle

    # ... 既有字段 (resume_session_id, max_turns, run_id, ...) ...
```

### 1.3 默认值矩阵

各字段 `None` 时的回退策略：

| 字段 | None → 回退 | rationale |
|---|---|---|
| `total_timeout_s` | `timeout_s` 若为 None 用 1800 | 30 分钟默认；与 `workflow_orchestrator.py:68` 的 1_800_000 ms 对齐 |
| `handshake_timeout_s` | 30s | 与 `backend_registry` 的 `codex app-server` 探针 2s 上限保持数量级 |
| `first_turn_timeout_s` | 120s | 单 turn 思考 + 工具 + 思考 |
| `inactivity_timeout_s` | `stall_timeout_s` 若为 None 用 300 | clawcodex 旧路径已有 stall_timeout 语义 |
| `idle_watchdog_timeout_s` | `total_timeout_s` | watchdog 不能比总时长长 |

### 1.4 校验

- `total_timeout_s > max(handshake_timeout_s, first_turn_timeout_s, inactivity_timeout_s)` — 总时长必须包裹各阶段超时
- `idle_watchdog_timeout_s >= total_timeout_s` — 不允许
- 校验在 `SessionSpec.__post_init__` 中做，发出 `ValueError`

### 1.5 改动清单

- `src/orchestratord/spi/backend.py:41-43` — 现有 3 个字段加 `deprecated` 注释（不改语义）
- `src/orchestratord/spi/backend.py:48-52` — 新增 5 字段 + `__post_init__`
- `src/orchestratord/spi/backend.py` 顶部 docstring — 增 1 段解释 5 级语义（参考 multica `agent.go:40-61`）

### 1.6 验收

- 单元测试：`SessionSpec(..., total_timeout_s=10, handshake_timeout_s=20)` 抛 `ValueError`
- 现有测试套不破：`timeout_s=None, stall_timeout_s=None` 仍合法

---

## 2. 方案 B — ResumeStatus 三态

### 2.1 目标

把 `AgentSession.close()` / `create_session(resume=...)` 路径的成功/失败结构化为三态，编排侧按态分支。

### 2.2 新增类型

```python
# src/orchestratord/spi/session.py — 新增
import enum

class ResumeStatus(enum.Enum):
    """Resume outcome signal.

    三态而非二态的设计动机：部分后端（如 dsh / opencode）无法
    探测 transcript 是否仍在服务端保留；这种"我不知道"应当被
    显式表达而非默认为 False/True。参考 multica agent.go:203-231
    与 agent.go:350-364。
    """
    RESUMED = "resumed"               # 成功恢复，可继续 send()
    REJECTED = "rejected"             # 显式拒绝（transcript 已被 GC / 配额满 / 主动撤销）
    UNDETECTABLE = "undetectable"     # 后端无法判定（probe 失败 / 协议无 resume 探测）


@dataclass
class SessionResult:
    """Final result returned by AgentSession.close() / when consumed."""
    status: ResumeStatus
    final_text: str | None = None
    last_event_seq: int | None = None
    resume_target_session_id: str | None = None  # 仅 RESUMED 时填
    reason: str | None = None                    # 仅 REJECTED / UNDETECTABLE 时填
    error_code: str | None = None                # 自由文本，匹配 backend 的错误分类
```

### 2.3 AgentSession 接口扩展

```python
# src/orchestratord/spi/session.py — AgentSession Protocol 增方法
@runtime_checkable
class AgentSession(Protocol):
    # ... 既有方法 ...
    async def probe_resume(self) -> ResumeStatus:
        """在不 send() 的前提下探测目标 transcript 是否仍可恢复。

        Returns:
            ResumeStatus.RESUMED       — 可恢复
            ResumeStatus.REJECTED      — 已被服务端 GC 或配额满
            ResumeStatus.UNDETECTABLE  — 后端协议无 resume 探测能力

        默认实现（mixin 提供）：
            - clawcodex: 真探测
            - codex AppServer: 真探测
            - dsh: UNDETECTABLE
            - hermes: REJECTED（hermes 不支持跨进程 resume）
            - opencode: 真探测（`session/load` 走 MCP）

        校验：
        - 所有 backend 必须在 capability bits 里声明
          `resume_detection: bool`（capabilities 当前 9 位；
          ADR-002 引入第 9 位 `goal_mode`，本设计新增第 10 位）。
        """
        ...
```

### 2.4 BackendCapabilities 增 1 位

```python
# src/orchestratord/spi/capabilities.py — 增字段
@dataclass(frozen=True)
class BackendCapabilities:
    streaming_deltas: bool
    interrupt: bool
    approval_hooks: bool
    cost_reporting: bool
    resume_session: bool            # 现有
    goal_mode: bool                 # ADR-002
    # ... 既有 ...
    resume_detection: bool = False  # 方案 B 新增
```

### 2.5 改动清单

- `src/orchestratord/spi/session.py` — 新增 `ResumeStatus` enum + `SessionResult` dataclass + `probe_resume()` Protocol 方法
- `src/orchestratord/spi/capabilities.py` — 加 `resume_detection` 字段
- `src/orchestratord/agent_runner.py:1574` — 在调 `create_session(resume_session_id=...)` 后立刻调 `probe_resume()`，按状态分支
- `src/orchestratord/orchestrator.py:4763-4790` — 重构 `_handle_resume_control` 类方法：用 `ResumeStatus` 替换 ad-hoc try/except
- 各 backend 包：实现 `probe_resume()`（具体见 §3）

### 2.6 验收

- 单元测试：5 个 backend 的 `probe_resume()` 在 spy/mock 模式下返回 3 种状态
- `test_capability_drift.py`：新增对 `resume_detection` 位的校验
- `failure_messages.py:41` 改为基于 `ResumeStatus` 而非 `timeout_s` 计算文案

---

## 3. 方案 C — BackendRunner 落地超时 + 适配 probe_resume

### 3.1 目标

`BackendRunner`（`src/orchestratord/backend_runner.py:275-306`）是所有 SPI backend 的统一入口；它在 session 创建后把 5 级超时交给 backend 适配，并在 `events()` 上挂 inactivity watchdog。

### 3.2 设计

```python
# src/orchestratord/backend_runner.py — 适配示例
class BackendRunner:
    async def _run_session(self, backend, spec, ...):
        # 1. 解析超时（None → 默认）
        timeouts = _resolve_timeouts(spec)  # 返回 5 元 TimeoutBundle

        # 2. 创建 session
        session = backend.create_session(spec)

        # 3. 若 resume_session_id，先 probe
        if spec.resume_session_id:
            status = await session.probe_resume()
            if status is ResumeStatus.REJECTED:
                yield _resume_rejected_event(spec, "transcript-gone")
                return
            if status is ResumeStatus.UNDETECTABLE:
                # 尝试 send() — 若失败 fallback 到 error
                pass

        # 4. 把 5 级超时挂到 session 的 send/iterate 包装
        guarded = _wrap_with_timeouts(session, timeouts)

        async for env in guarded.events():
            yield env
```

### 3.3 每个 backend 的适配

| backend | probe_resume 实现 | 5 级超时实现 |
|---|---|---|
| clawcodex | 真探测：`extensions.api.query.QueryRunner` 有 transcript 探针 | 用 SDK 的 turn-level timeout 配置 + 上层 watchdog 协程 |
| codex AppServer | 真探测：`POST /sessions/{id}` → 200/404 | 同上 |
| codex Cli | UNDETECTABLE（无跨进程状态） | spawn-per-turn 模式，watchdog 不可用 → 总超时靠 OS signal |
| dsh | UNDETECTABLE（SDK 无 resume 协议） | 子进程整体由 `total_timeout_s` 守 |
| hermes | REJECTED（明确不支持） | spawn-per-turn，`total_timeout_s` 守每一 turn |
| opencode | 真探测：`session/load` MCP 调用 | httpx async timeout + SSE 心跳 inactivity |

### 3.4 改动清单

- `src/orchestratord/backend_runner.py` — `_resolve_timeouts()` + `_wrap_with_timeouts()` + `_resume_rejected_event()`
- `backends/orchestratord-clawcodex/src/orchestratord_clawcodex/session.py` — 实现 `probe_resume()`
- `backends/orchestratord-codex/src/orchestratord_codex/app_server_session.py` — 同上
- `backends/orchestratord-opencode/src/orchestratord_opencode/session.py` — 同上
- `backends/orchestratord-dsh/src/orchestratord_dsh/session.py` — 实现 `probe_resume()` 返回 `UNDETECTABLE`
- `backends/orchestratord-hermes/src/orchestratord_hermes/session.py` — 实现 `probe_resume()` 返回 `REJECTED`
- `tests/test_orchestrator_resume.py` — 重构为 3 态矩阵

### 3.5 验收

- 单测：`BackendRunner` 在 5 个 backend 的 fake 实现上跑出 5 级超时的正确触发
- 集成测试：构造一个 fake session `events()` 阻塞 N 秒，验证对应的超时事件被发出
- `test_capability_drift.py` 新增 `resume_detection` 位校验：clawcodex/codex/opencode 必须 `True`，dsh/hermes 必须 `False`

---

## 4. 方案 D — Failure messages 路由

### 4.1 目标

`failure_messages.py:41-43` 当前基于 `timeout_ms` 单一字段构造失败消息。改为基于 `ResumeStatus` + `error_code` 路由：

### 4.2 设计

```python
# src/orchestratord/failure_messages.py — 重构
def build_failure_message(
    *, status: ResumeStatus, error_code: str | None, timeouts: TimeoutBundle
) -> str:
    """根据 resume 状态 + error_code + 实际经过的超时阶段构造消息。

    优先级：
    1. REJECTED → "transcript 已被 GC" / "配额已满" / "撤销"
    2. UNDETECTABLE + error_code="handshake_timeout" → "agent 未能在 X 秒内启动"
    3. UNDETECTABLE + error_code="inactivity_timeout" → "X 秒无 token 输出"
    4. UNDETECTABLE + error_code="total_timeout" → "整轮超过 X 秒"
    5. 兜底 → "未知失败"
    """
```

### 4.3 改动清单

- `src/orchestratord/failure_messages.py:41-43` — 整段重写
- `src/orchestratord/orchestrator.py` — 失败处理点改为接收 `ResumeStatus`
- `tests/test_failure_messages.py` — 增加 3×4=12 矩阵用例

### 4.4 验收

- 12 矩阵全过
- `orchestrator.py` 主路径 5 个失败分支覆盖：RESUMED 后正常 / REJECTED / handshake 失败 / first_turn 失败 / inactivity 失败 / total 失败 / UNDETECTABLE 兜底

---

## 5. 完整改动清单

| 文件 | 改动 | 行数估计 |
|---|---|---|
| `src/orchestratord/spi/backend.py` | 5 字段新增 + `__post_init__` + 顶部 docstring | +30 |
| `src/orchestratord/spi/session.py` | `ResumeStatus` enum + `SessionResult` + `probe_resume()` Protocol | +50 |
| `src/orchestratord/spi/capabilities.py` | 加 `resume_detection` 字段 | +2 |
| `src/orchestratord/backend_runner.py` | `_resolve_timeouts()` + `_wrap_with_timeouts()` + resume 探测 | +120 |
| `src/orchestratord/failure_messages.py` | 重写为状态路由 | +60 |
| `src/orchestratord/orchestrator.py` | 失败分支按 `ResumeStatus` | +30 |
| `src/orchestratord/orchestrator.py:4763-4790` | 重构 `_handle_resume_control` 块 | +40 |
| `src/orchestratord/agent_runner.py:1574` | 在 `create_session` 后调 `probe_resume()` | +10 |
| `backends/orchestratord-clawcodex/.../session.py` | `probe_resume()` 真探测 | +30 |
| `backends/orchestratord-codex/.../app_server_session.py` | `probe_resume()` 真探测 | +30 |
| `backends/orchestratord-opencode/.../session.py` | `probe_resume()` 真探测 | +30 |
| `backends/orchestratord-dsh/.../session.py` | `probe_resume()` 返回 UNDETECTABLE | +10 |
| `backends/orchestratord-hermes/.../session.py` | `probe_resume()` 返回 REJECTED | +10 |
| `tests/test_capability_drift.py` | 加 `resume_detection` 位校验 | +20 |
| `tests/test_failure_messages.py` | 12 矩阵用例 | +120 |
| `tests/test_orchestrator_resume.py` | 重构为 3 态 | +60 |
| **新测试** `tests/test_graded_timeouts.py` | 5 级超时独立触发 | +200 |

总计改 13 文件 + 新 1 测试；代码约 850 行。

---

## 6. 关键代码预览

### 6.1 SessionSpec 增字段

```python
@dataclass
class SessionSpec:
    # ... 既有字段 ...

    # === 5 级超时（方案 A）===
    total_timeout_s: float | None = None
    handshake_timeout_s: float | None = None
    first_turn_timeout_s: float | None = None
    inactivity_timeout_s: float | None = None
    idle_watchdog_timeout_s: float | None = None

    def __post_init__(self) -> None:
        # 1. 兼容：把 deprecated timeout_s / stall_timeout_s 映射到新字段
        if self.timeout_s is not None and self.total_timeout_s is None:
            self.total_timeout_s = self.timeout_s
        if self.stall_timeout_s is not None and self.inactivity_timeout_s is None:
            self.inactivity_timeout_s = self.stall_timeout_s

        # 2. 校验总时长包裹子超时
        subs = [
            self.handshake_timeout_s,
            self.first_turn_timeout_s,
            self.inactivity_timeout_s,
        ]
        subs = [v for v in subs if v is not None]
        if self.total_timeout_s is not None and subs:
            if any(s > self.total_timeout_s for s in subs):
                raise ValueError("sub-timeout exceeds total_timeout_s")
```

### 6.2 ResumeStatus 核心

```python
class ResumeStatus(enum.Enum):
    RESUMED = "resumed"
    REJECTED = "rejected"
    UNDETECTABLE = "undetectable"
```

---

## 7. 验收标准（Verification）

- [ ] **`SessionSpec` 5 字段校验单测全过**：合法构造不抛；非法构造（子超时 > 总超时）抛 `ValueError`
- [ ] **5 个 backend 各自有 `probe_resume()` 实现**：单测覆盖 RESUMED / REJECTED / UNDETECTABLE 三态
- [ ] **`BackendRunner` 单测**：5 级超时各自独立触发并产生对应 error_code
- [ ] **`failure_messages` 12 矩阵全过**：3 状态 × 4 error_code
- [ ] **`test_capability_drift.py`** 加 `resume_detection` 位校验：clawcodex/codex/opencode 必须 `True`，dsh/hermes 必须 `False`
- [ ] **现有测试套不破**：94 个 test 文件全部仍 PASS
- [ ] **ADR-003 起草**：本设计的决议形成 `ADR-003-graded-timeouts-and-resume.md`，含本设计 §1.4 默认值矩阵 + §2.3 backend 适配表 + 后续"分级超时与 Cap10 位"两条决策

---

## 8. 范围外（Out of Scope）

1. **clawcodex 内核改造**：不重写 `extensions.api.query.QueryRunner`，只在其上薄包一层 `probe_resume()`
2. **超时预算编排**：orchestrator 不强制各 turn 累计 ≤ total_timeout_s，由 backend 自行管理
3. **跨 session resume**：本次设计仅考虑同 session_id；新 session_id 复用旧 transcript 不在范围
4. **probe_resume() 的缓存**：每次 send 前都探测，不缓存 — 缓存属后续优化

---

## 9. 后续（Future Work）

- 把 5 级超时抽成 `TimeoutBundle` 数据类，便于统一 logging 与 metrics
- `BackendCapabilities` 加 `cost_event_emission` 等位（参见 `DESIGN_backends_hardening.md` §5.3 deferred 项）
- 在 `chat_gateway.py` 把 `ResumeStatus` 暴露给前端：UI 可显示"恢复失败"而非静默重试

---

## 10. 参考资料

- multica `agent.go:40-61` — `ExecOptions` 五超时
- multica `agent.go:203-231` — `Result.ResumeRejected` 语义
- multica `agent.go:350-364` — `ResumeRejectionUndetectable`
- orchestratord 现有 SPI：`src/orchestratord/spi/{backend,session,capabilities}.py`
- ADR-001：[Backend Hardening §3](ADR-001-backends-hardening.md)
- ADR-002：[Goal Mode 引入 GOAL_* event 扩 cap 位](ADR-002-goal-mode.md)（参考本设计如何扩 cap 位）