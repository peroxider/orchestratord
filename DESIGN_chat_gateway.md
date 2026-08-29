# 聊天网关方案 — 设计文档

> **状态：** 临时设计稿，待评审。
> **目标：** 让任意后端的 agent 在编排执行过程中，其输出以聊天界面（类 ChatGPT/Kimi/DeepSeek）实时可见，且可双向对话。
> **不解决：** WebSocket 传输、对外网暴露与多用户鉴权、外部 Visualizer 集成、现有 dashboard 监控页改造。

---

## 0. 背景与现状摘要

### 0.1 需求拆解

"聊天框方式可见 + 可交互对话"拆成四个可验收的子能力：

1. **流式可见** — agent 输出逐字（逐 delta）推到浏览器，打字机效果；
2. **历史回放** — 中途打开页面能看到之前的完整对话；
3. **双向对话** — 用户发消息能进入 agent 的对话上下文并影响后续行为；
4. **后端无关** — 五个后端（clawcodex / codex / dsh / hermes / opencode）行为一致，不因能力位差异缺文本。

### 0.2 已有积木（均经代码核实）

| 积木 | 位置 | 现状 |
|---|---|---|
| 归一化事件流 | `src/orchestratord/spi/session.py:29-34` + `spi/events.py:26-33` | `events()` 异步迭代器产出带 `seq` 的 `EventEnvelope`，注释明确设计了"任意 seq 可重放"（snapshot-tolerant）——正是聊天回放需要的性质，只是一直没被 web 层用上 |
| 逐字实时广播 | `agent_runner.py:1508-1519` → `control_socket.py:198` | 每个 `TextDelta / ToolCall / ToolResult` 事件**实时**以 JSON 行广播到 `{workspace}/.run_control/{run_id}.sock` 的所有连接客户端，支持多客户端并发 |
| 广播帧形状 | `runner_utils.py:26-80` | `_event_to_broadcast_dict` 定义各事件 payload：TextDelta=`{content}`、ToolCall=`{tool_name, tool_use_id, params, approved}`、ToolResult=`{tool_name, tool_use_id, result}` |
| 消息注入（入站） | `runner_utils.py:257-333` | `inject` 命令：写 transcript UserMessage + 排 pending 队列 + 回 `InjectDelivered` 确认帧；命令以 ~60ms 间隔排空（`agent_runner.py:427-447`） |
| 改指令（入站） | `runner_utils.py:131-144` + `agent_runner.py:1199-1200` | `resume` 带 payload → `prompt_override` → 下一轮 prompt 被替换。runner 层机制，天然后端无关 |
| 控制动词 | `control_socket.py:9-18` | pause / resume / inject / stop / detach / takeover / flush_transcript，全部已实现 |
| run 发现 | IssueRegistry | issue_id → (run_id, workspace_path)，takeover 命令已用这条查找（`cli/takeover.py:43-80`） |
| SSE 前端基建 | `cli/dashboard.py` | EventSource 客户端（`:1306`）、issue 轮询、transcript 历史回放（`event_tailer.py:106-120`）都有 |
| 入站语义先例 | `im_gateway_client.py:7-9` | IM 侧已把 `followUp → queue_pending_message`、`pause/resume/stop → 控制动词`的语义映射做过一遍，可直接复用同一套语义 |

### 0.3 必须先修的两个缺口

**缺口 1（正确性，blocking）：非流式后端的文本会被丢弃。**
`_SpiEventAdapter` 只翻译 `TEXT_DELTA`，`EventKind.TEXT` 直接 ignore（`agent_runner.py:292`，注释写着"clawcodex only emits TEXT_DELTA"）。而 dsh / hermes / codex-Cli / opencode 降级路径发的恰恰是 `TEXT`。后果：这些后端走 AgentRunner 路径时，`session.output_text` 不累积、transcript 无文本、控制 socket 无 TextDelta 广播——**聊天里什么都看不到**。同时 README 能力矩阵承诺的"streaming_deltas 位关闭时核心把整段 text 拆成伪 delta"（`spi/capabilities.py:23`）只有文档、没有实现（全库 grep 无拆分代码）。

**缺口 2（架构，Phase 2）：`BackendRunner` 路径没有交互能力。**
orchestrator 以 SPI backend 装配时走 `BackendRunner`（`orchestrator.py:202-208`），它只分发事件给 `progress_reporter`（`backend_runner.py:275-306`，TEXT/TEXT_DELTA 都处理），**没有** control socket、没有 transcript、没有 inject。另外现有 pending 队列实现 `from src.tasks.local_agent import queue_pending_message`（`runner_utils.py:296`）是 clawcodex 的模块——纯 orchestratord 安装下 inject 退化成写 `.operator_hints.md`，而该文件只有 clawcodex 后端会读。通用的注入语义应落在 runner 层的"下一轮 prompt 拼接"：runner 在 turn 循环内每轮重建 SPI 会话（`agent_runner.py:1428-1430`），把用户消息拼进下一轮 prompt 对所有后端成立。

### 0.4 方案总览

| # | 方案 | 范围 | 解决 |
|---|---|---|---|
| A | SPI 事件归一化（TEXT→伪 delta 装饰器） | `spi/` + `backend_registry.py` | 缺口 1 |
| B | 后端无关的 followUp 注入 | `agent_runner.py` / `runner_utils.py` / `session_state.py` / `control_socket.py` | 双向对话（通用档） |
| C | Chat 网关（control socket ↔ SSE/HTTP 桥） | 新增 `chat_gateway.py` + `cli/dashboard.py` 路由 | 流式可见 + 历史回放 |
| D | 聊天前端（`/chat` 页面） | `cli/dashboard.py` 内嵌页面 | 聊天体验 |
| E | Phase 2：seq 日志重放 + BackendRunner 统一 + 审批 UI | daemon 侧 | 完备性 |

---

## 1. 方案 A — SPI 事件归一化（TEXT → 伪 TEXT_DELTA）

### 1.1 目标

把 `spi/capabilities.py:23` 文档承诺但未实现的核心降级路径真正落地：`streaming_deltas=False` 的后端发出的整段 `TEXT`，在 SPI 会话层被切分成若干 `TEXT_DELTA`。此后**所有消费者只面对一种文本事件**，"后端无关"的流式可见成立。

### 1.2 设计原则

- **装饰器而非改后端**：五个后端包不动，归一化在核心侧一个包装点完成——这正是 README "Backends never self-degrade; the core enforces" 的原意。
- **纯透传保证**：对只发 `TEXT_DELTA` 的会话，包装器是零语义变化的透传（seq 重排除外）。
- **无新增依赖**。

### 1.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/orchestratord/spi/degradation.py` | 新建。`DegradingSession`（会话装饰器）+ `DegradingBackend`（backend 装饰器）+ `_split_chunks`（按行切、单行超长硬切，块长约 200 字符）。 |
| `src/orchestratord/backend_registry.py` | `discover_backends()` 返回前统一包 `DegradingBackend`（capabilities 原样透传）。 |
| `src/orchestratord/agent_runner.py` | lazy 创建 `ClawcodexBackend` 的分支（`:327-330` 附近）同样包一层。 |

### 1.4 关键代码改动预览

```python
# src/orchestratord/spi/degradation.py (新)
from dataclasses import replace
from orchestratord.spi.events import EventEnvelope, EventKind

_CHUNK_CHARS = 200

class DegradingSession:
    """把 streaming_deltas=False 会话的 TEXT 事件切分成伪 TEXT_DELTA。

    SPI 承诺的核心降级路径（capabilities.py 文档）在此落地。
    对只发 TEXT_DELTA 的会话是纯透传。seq 被整体重排以保证
    拆分后仍是全序递增（snapshot-tolerant 语义保持）。
    """

    def __init__(self, inner):
        self._inner = inner
        self._seq = 0

    @property
    def session_id(self): return self._inner.session_id
    @property
    def capabilities(self): return self._inner.capabilities
    async def send(self, content): await self._inner.send(content)
    async def interrupt(self): await self._inner.interrupt()
    async def approve(self, request_id, decision):
        await self._inner.approve(request_id, decision)
    async def close(self): await self._inner.close()

    def _reseq(self, env, kind, payload):
        self._seq += 1
        return replace(env, seq=self._seq, kind=kind, payload=payload)

    async def events(self):
        async for env in self._inner.events():
            if env.kind is EventKind.TEXT:
                text = env.payload.get("text", "")
                for chunk in _split_chunks(text, _CHUNK_CHARS):
                    yield self._reseq(env, EventKind.TEXT_DELTA,
                                      {"text": chunk, "delta": chunk})
            else:
                self._seq += 1
                yield replace(env, seq=self._seq)


class DegradingBackend:
    """create_session 统一包 DegradingSession；capabilities 原样透传。"""

    def __init__(self, inner): self._inner = inner
    @property
    def name(self): return self._inner.name
    def capabilities(self): return self._inner.capabilities()
    def create_session(self, spec):
        return DegradingSession(self._inner.create_session(spec))
```

> 注：`EventEnvelope` 是 frozen dataclass，重排 seq 用 `dataclasses.replace`。包装后 `agent_runner.py:292` 的"TEXT: ignore"注释仍然成立——TEXT 不再到达 adapter。

### 1.5 失败模式与回退

| 情况 | 行为 |
|---|---|
| 后端只发 TEXT_DELTA | 纯透传，唯一差异是 seq 被重排为连续整数（原本也是连续的，通常无变化） |
| 后端 TEXT 与 TEXT_DELTA 混发 | 各自透传/切分，互不干扰 |
| TEXT payload 为空字符串 | 不产 delta，直接跳过 |
| 包装器自身异常 | 会话创建失败在 `create_session` 处正常抛出——与未包装行为一致，不引入静默吞错 |

### 1.6 测试

新增 `tests/test_spi_degradation.py`：

1. **切分正确性**：mock 内层会话发一条 `TEXT("hello\nworld")` → 断言收到 ≥2 个 `TEXT_DELTA`，拼接还原原文；
2. **seq 全序**：TEXT 拆分后再来一条 TOOL_CALL，断言 seq 单调递增无重复；
3. **透传不变**：内层只发 TEXT_DELTA/TOOL_CALL → 事件种类与 payload 逐一相等；
4. **组合契约**：`DegradingSession` 输出喂给 `_SpiEventAdapter` → 流中不含 `EventKind.TEXT`；
5. **单行超长**：一条 1000 字符无换行 TEXT → 按块长硬切，拼接还原。

### 1.7 风险

- **行为变化**：非流式后端（dsh/hermes/codex-Cli/opencode 降级）的文本从"被丢弃"变成"出现在 output_text / transcript / 广播流"。这是修复而非回归，但 429/停滞防护会开始看到这些文本（`agent_runner.py:287-291` 的 ERROR→TextDelta 路径同理），需要在测试中显式覆盖。
- **backend_runner 的 sink 回调变化**：`backend_runner.py:279-291` 对 TEXT 调 `on_text`、对 TEXT_DELTA 调 `on_text_delta`；包装后非流式后端只触发后者。两条分支都累积 `output_text`，总量不变；若有 sink 对 `on_text` 有特殊行为需逐一检查。
- **既有测试断言**：若现有测试断言了 dsh/hermes 流里存在 `TEXT` kind，需同步更新。

---

## 2. 方案 B — 后端无关的 followUp 注入

### 2.1 目标

让"发消息给运行中的 agent"有一条**对所有后端成立**的通路：消息进入 per-session 待注入队列，在下一个 turn 边界拼进下一轮 prompt。区别于现有两条入站路径——`inject` 依赖 clawcodex 的 `src.tasks.local_agent`（纯安装下无效），`resume` 的 `prompt_override` 是整体替换（会吃掉原始任务）。

### 2.2 语义分档

| 档位 | 机制 | 后端无关性 | 适用 |
|---|---|---|---|
| `followUp`（默认，本方案新增） | 待注入队列 → 下一轮 prompt **追加** | 全部后端（runner 每 turn 重建会话，`agent_runner.py:1428`） | 聊天输入框的默认行为 |
| `inject`（既有） | transcript UserMessage + pending 队列（工具轮边界生效） | clawcodex 在场时 mid-turn 生效；否则退化写 operator hints | 增强档，保留 |
| `resume(payload)`（既有） | `prompt_override` 整体替换下一轮 prompt | 全部后端 | "打断并改指令" |
| `pause/resume/stop`（既有） | 控制动词 | 全部后端 | 控制按钮 |

### 2.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/orchestratord/session_state.py` | 新增字段 `_pending_followups: list[str]`（default_factory=list）。 |
| `src/orchestratord/control_socket.py` | `ControlCmd` Literal 增加 `"followup"`。 |
| `src/orchestratord/runner_utils.py` | `_drain_control_commands` 新增 `followup` 分支：transcript 落 UserMessage（复用 inject 的落盘块）→ 追加 `_pending_followups` → 广播 `FollowupQueued` 确认帧。 |
| `src/orchestratord/agent_runner.py` | 下一轮 prompt 组装处（`:1199` 附近）拼接 pending followups 并清空；顺带补上 `TurnComplete / SessionComplete` 的控制 socket 广播（现在只有 Paused/Resumed/TextDelta/ToolCall/ToolResult/InjectDelivered 在播）。 |

### 2.4 关键代码改动预览

```python
# runner_utils.py — _drain_control_commands 新增分支
elif cmd.cmd == "followup":
    # 1) transcript 落一条 UserMessage（复用 inject 的落盘逻辑，
    #    takeover REPL 与聊天历史回放都能看到这条用户消息）
    _write_user_message_to_transcript(session, cmd.payload)
    # 2) 排入 runner 层待注入队列 —— 后端无关
    session._pending_followups.append(cmd.payload)
    # 3) 广播确认帧（前端打勾的依据）
    _emit_frame(session, {
        "type": "FollowupQueued",
        "data": {"snippet": cmd.payload[:80]},
    })
```

```python
# agent_runner.py — 下一轮 prompt 组装（:1199 附近）
if session.prompt_override:
    prompt = session.prompt_override
    session.prompt_override = None
if session._pending_followups:
    followups = "\n".join(f"- {m}" for m in session._pending_followups)
    prompt = f"{prompt}\n\n[Operator follow-up]\n{followups}"
    session._pending_followups.clear()
```

### 2.5 失败模式与回退

| 情况 | 行为 |
|---|---|
| run 已结束（socket 已关闭） | 网关侧提交失败 → HTTP 409，前端冻结为只读视图 |
| followup 到达时 run 正在最后一轮 | 拼进最后一轮 prompt；若循环不再继续，消息留在 transcript 中作为记录（不丢失，但不再影响 agent） |
| 排空线程 60ms 窗口内多条 followup | 队列按序全部拼入同一轮 prompt |
| transcript 存储不可用（无 clawcodex adapters） | 落盘块 best-effort 跳过，队列照常生效（与 inject 的容错一致） |

### 2.6 测试

新增 `tests/test_followup_injection.py`：

1. **命令解析**：socket 客户端发 `{"cmd":"followup","payload":"改用方案B"}` → `_pending_followups` 含该消息，transcript 出现 UserMessage，`FollowupQueued` 帧广播；
2. **拼接生效**：构造带 pending followup 的 session 跑两轮 → 第二轮收到的 prompt 含 `[Operator follow-up]` 段，且队列清空；
3. **不覆盖原始任务**：原始 prompt 文本完整保留在拼接结果中（对比 `prompt_override` 的整体替换）；
4. **多条合并**：两条 followup → 同一轮 prompt 两个条目。

### 2.7 风险

- **生效延迟 = 当前 turn 剩余时长**：turn 边界语义下，长 turn 中 followUp 要等工具轮走完。可接受——ChatGPT 的"生成中发消息"也是下一轮处理；需要立即打断时用 pause/resume 组合。
- **prompt 污染**：拼接格式必须对模型明确标注为 operator 指令，避免 agent 把它当成任务本身的一部分。固定用 `[Operator follow-up]` 标头。

---

## 3. 方案 C — Chat 网关（control socket ↔ SSE/HTTP 桥）

### 3.1 目标

在 dashboard 进程内新增一个桥接组件：出站把 per-run 控制socket的实时帧转成 SSE 推给浏览器；入站把 HTTP POST 转成控制命令写回 socket。daemon 侧零新增网络面（复用既有 UDS），网关崩溃绝不影响 agent 运行——这正是 `control_socket.py` "坏连接永不拖垮 agent" 设计纪律的延续。

### 3.2 设计原则

- **网关 = 控制socket的又一个客户端**：与 takeover/inject 等 CLI 工具同级，不赋予特权路径。
- **生命周期镜像 `EventTailerManager`**：`sync_active_run_ids()` 起/停 per-run 连接线程，与 dashboard 现有 0.5s 快照刷新节奏复用（`dashboard.py:341`）。
- **大内容不上行帧**：遵守 control socket "小帧 only" 纪律（`control_socket.py:16-18`）：ToolResult 广播截断 ~4KB 标 `truncated:true`，全文走惰性 GET。
- **异步桥**：UDS 是 asyncio API，dashboard 是 `ThreadingHTTPServer` 同步线程模型——网关内起一个专属 asyncio loop 线程管理所有 run 连接，与 HTTP 线程通过 thread-safe queue 交换（与 `event_tailer.py` 的线程模型一致）。

### 3.3 API 面（挂在 dashboard 现有端口）

| Method | Path | 作用 |
|---|---|---|
| GET | `/chat` | 聊天页 UI（方案 D） |
| GET | `/api/runs` | 活跃 run 列表（IssueRegistry 派生，含 run_id/issue_id/backend/status） |
| GET | `/api/runs/{run_id}/events` | SSE：先 `history` 帧（transcript 回放），`boundary` 帧后转实时 `frame` 流；心跳复用现有 ping 模式 |
| POST | `/api/runs/{run_id}/messages` | `{"text": "..."}` → `followup` 命令 → 202；送达确认以 `FollowupQueued`/`InjectDelivered` 帧经 SSE 返回 |
| POST | `/api/runs/{run_id}/pause` `/resume` `/stop` | 控制动词；`resume` 可带 `{"message": "..."}` 实现"打断改指令"（prompt_override） |
| GET | `/api/runs/{run_id}/tool-results/{call_id}` | 从 transcript/events.ndjson 读工具结果全文（截断帧的补全通道） |

run 发现：dashboard 每次 `refresh_snapshot` 时从 IssueRegistry 取活跃 run，计算 `{workspace}/.run_control/{run_id}.sock`，存在即确保连接。

### 3.4 关键代码改动预览

```python
# src/orchestratord/chat_gateway.py (新)
class _RunConnection:
    """一个 run 的 UDS 长连接：读帧 → 分发订阅者；写命令 → agent。"""

    def __init__(self, run_id: str, sock_path: Path, loop: asyncio.AbstractEventLoop):
        self.run_id = run_id
        self._sock_path = sock_path
        self._loop = loop
        self._subscribers: list[queue.Queue] = []
        self._writer: asyncio.StreamWriter | None = None

    async def _read_loop(self) -> None:
        reader, writer = await asyncio.open_unix_connection(str(self._sock_path))
        self._writer = writer
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break                       # run 结束，socket 被关闭
                frame = json.loads(line)
                for q in list(self._subscribers):
                    q.put_nowait(frame)
        finally:
            self._broadcast({"type": "RunEnded", "data": {"run_id": self.run_id}})

    def submit(self, verb: str, payload: str = "") -> bool:
        """HTTP 线程调用：线程安全地写一条控制命令。"""
        if self._writer is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(
            self._write_line(verb, payload), self._loop)
        return fut.result(timeout=2.0)


class ChatGateway:
    """per-run 连接管理，API 镜像 EventTailerManager。"""

    def sync_active_run_ids(self, run_id_to_sock: dict[str, Path]) -> None: ...
    def subscribe(self, run_id: str) -> queue.Queue: ...
    def unsubscribe(self, run_id: str, q: queue.Queue) -> None: ...
    def read_history(self, run_id: str, workspace: Path) -> list[dict]: ...
    def send_message(self, run_id: str, text: str) -> bool: ...
    def control(self, run_id: str, verb: str, payload: str = "") -> bool: ...
```

```python
# cli/dashboard.py — SSE 处理器（挂到 DashboardHandler）
def _stream_chat_events(self, run_id: str) -> None:
    gw: ChatGateway = self.__class__.chat_gateway
    live = gw.subscribe(run_id)                  # ① 先订阅（缓冲实时帧）
    history = gw.read_history(run_id, ws_path)   # ② 再读历史
    self._write_sse({"t": "history", "messages": history})
    self._write_sse({"t": "boundary"})
    while True:
        try:
            frame = live.get(timeout=self.state.snapshot_interval)
        except queue.Empty:
            self.wfile.write(b": ping\n\n"); self.wfile.flush()
            continue
        self._write_sse({"t": "frame", "frame": frame})
```

> 历史读取复用 `event_tailer.py` 的 `_SessionTailer` 解析逻辑，新增不经过共享队列的直读方法（`load_historical` 的变体）。**边界缝隙**：跨"订阅/读历史"边界的 turn 可能同时出现在历史（整段）与缓冲（delta）中——v1 接受罕见重复（前端按 turn 收束）；Phase 2 的 seq 日志以单调序彻底消除。

### 3.5 失败模式与回退

| 情况 | 行为 |
|---|---|
| socket 文件不存在（run 未启动/已结束） | `sync_active_run_ids` 不建连接；POST 返回 409 `run not active` |
| run 中途结束（readline 返回 EOF） | 广播 `RunEnded` 帧给订阅者，前端转只读视图；连接对象保留供历史查询 |
| 网关 asyncio loop 线程崩溃 | 仅影响聊天功能；dashboard 主服务与 agent 运行不受影响（进程内隔离 + 全链路 try/except 纪律） |
| 浏览器断开 SSE | `wfile.write` 抛异常 → 处理器退出 → `unsubscribe` 清理；UDS 连接保持（其他订阅者还在） |
| ToolResult 帧 > 4KB | 网关截断 payload、置 `truncated: true`，前端点开卡片再 GET 全文 |
| 同一浏览器多标签页 | 各自独立 SSE 订阅，control socket 本就支持多客户端广播 |

### 3.6 测试

新增 `tests/test_chat_gateway_bridge.py`：

1. **桥接往返**：用真实 `ControlSocket` 起一个 mock UDS server → `ChatGateway` 订阅 → server 端 `send_event` → 断言订阅队列收到帧；
2. **命令提交**：网关 `submit("followup", "hi")` → mock server 端 `poll_commands` 收到 `ControlCommand(cmd="followup", payload="hi")`；
3. **生命周期**：`sync_active_run_ids` 增删 run → 连接建立/关闭；EOF 后 `RunEnded` 帧广播；
4. **截断**：超 4KB ToolResult 帧 → payload 截断且 `truncated: true`；
5. **E2E**（默认 skip）：`manual_e2e_chat_stream.py` — FakeBackend 跑一个 issue，`curl -N` SSE 断言收到 TextDelta 流。

### 3.7 风险

- **asyncio/线程桥接复杂度**：专属 loop 线程是主要复杂度来源。缓解：所有跨界交互只有两个原语（订阅 queue、`run_coroutine_threadsafe` 提交），不共享其他可变状态。
- **UDS only，无 Windows**：既有约束（`control_socket.py:15` 已声明 TCP fallback 属后续阶段），聊天网关继承该约束。
- **每 run 一条常驻连接**：并发 run 数量大时连接数 = run 数。本地单机场景（<20 并发）无压力。

---

## 4. 方案 D — 聊天前端（/chat 页面）

### 4.1 目标

一个内嵌单页聊天 UI，沿用 dashboard 现有"单文件 HTML/JS 内嵌模块 + stdlib 服务"模式（零构建、零新依赖）。

### 4.2 界面结构

```
┌─ runs ──────┬─ ISSUE-42 · opencode · turn 7 ── [暂停] [停止] ─┐
│ ISSUE-41 ●  │  [user]   帮我修复登录超时的 bug                 │
│ ISSUE-42 ▶  │  [agent] 我先看一下 auth 模块的实现…              │
│ ISSUE-43 ○  │  [tool]  Read  src/auth/session.py               │
│             │  [tool]  Grep  "timeout"                        │
│             │  [agent] 找到了，问题在 …（流式中）               │
│             ├─────────────────────────────────────────────────┤
│             │  [ 输入消息，Enter 发送（follow-up）…      ] [发送] │
└─────────────┴─────────────────────────────────────────────────┘
```

### 4.3 帧渲染规则

| 帧类型 | UI 行为 |
|---|---|
| `history` | 按完整气泡渲染历史对话（user / agent / tool 卡片） |
| `boundary` | 之后切流式渲染模式 |
| `TextDelta` | 追加到当前 assistant 流式气泡 |
| `ToolCallEvent` | 新建工具卡片（工具名 + 参数摘要，默认折叠） |
| `ToolResultEvent` | 回填卡片结果（截断；`truncated` 时显示"查看全文"） |
| `TurnComplete` | 收束当前流式气泡 |
| `Paused` / `Resumed` | 状态 chip 变化 |
| `InjectDelivered` / `FollowupQueued` | 对应发送中用户消息打勾 |
| `RunEnded` | 输入框禁用，视图冻结为只读 |

### 4.4 改动清单

| 文件 | 改动 |
|---|---|
| `src/orchestratord/cli/dashboard.py` | 新增 `CHAT_HTML` 模板 + `GET /chat` 路由 + 3.3 节的 API 处理器；`run()` 中初始化/清理 `ChatGateway`（挂进现有 signal/atexit 清理链）。 |

### 4.5 测试

1. **路由冒烟**：`GET /chat` 返回 200 + HTML；`GET /api/runs` JSON 结构；
2. **SSE 帧序列**：连接后依次收到 `history` → `boundary` →（实时帧）→ ping；
3. 交互（发消息/按钮）以 E2E 手动为主（方案 C 的 mock 测试已覆盖服务端语义）。

### 4.6 风险

- **dashboard.py 体量**：已 ~70KB，再内嵌聊天页会更大。缓解：聊天 UI 的 HTML/JS 独立为模块级常量（或 `chat_ui.py`），路由注册保持薄。

---

## 5. 方案 E — Phase 2：seq 日志重放 + 路径统一 + 审批 UI

Phase 1 完成后按需推进，三个独立子项：

### 5.1 ChatEventJournal（消除回放缝隙）

在 agent_runner 的广播点同步把每帧落盘 `{session_dir}/chat-events.ndjson`（daemon 侧写，保证无观察者时也不丢）；SSE 升级为 `?since=seq`——先从 journal 重放再续实时。中途连接不再丢失当前 turn 已输出的 delta（transcript 按 turn 整段落盘导致的 Phase 1 限制被彻底消除）。这兑现 `spi/events.py:26-33` 设计好的 snapshot-tolerant 语义。

### 5.2 BackendRunner 交互统一（修缺口 2）

control socket + `_pending_followups` + prompt 拼接下沉到 `session_state`/共享模块，`BackendRunner` 路径（`orchestrator.py:202-208`）同样可聊；顺带解除 `src.tasks.local_agent` 的 clawcodex 耦合（`runner_utils.py:296`）。

### 5.3 审批 UI

SPI 已有 `approve(request_id, decision)`（`spi/session.py:37-43`）；新增 `approve` 控制命令接入 runner 侧调用，聊天 UI 的工具卡片上出现 [允许]/[拒绝] 按钮。clawcodex / opencode / codex-As 三个后端都报了 `approval_hooks` 位，聊天界面是审批交互的天然归宿。

多 agent 模式（debate/pipeline）：SPI 的 send/events 分离本就是为多 agent 注入设计的，网关按 run_id 分键天然支持，前端加会话选择器即可。

---

## 6. 横向问题与排序建议

### 6.1 推荐落地顺序

```
[1] 方案 A SPI 归一化          ← 修正确性缺口，独立可测，改动最小
    │
[2] 方案 B followUp 注入       ← daemon 侧唯一改动，小而内聚
    │
[3] 方案 C Chat 网关           ← 纯新增进程内组件，依赖 [1][2]
    │
[4] 方案 D /chat 前端          ← 依赖 [3]
    │
[Phase 2] journal/seq → BackendRunner 统一 → 审批 UI
```

理由：A 是纯正确性修复且其它一切依赖它（没有 TEXT_DELTA 就没有聊天文本）；B 让入站语义就位；C/D 是纯新增，不碰 agent_runner 热路径。

### 6.2 工程纪律

- **agent_runner 事件循环是全局热路径**：所有新增广播/落盘点必须维持"异常永不外抛"的现有纪律（现有每个 `send_event` 调用点都包 try/except，新代码同样遵守）。
- **无新第三方依赖**：全部 stdlib（http.server / asyncio / queue / json）。

### 6.3 安全

默认绑 127.0.0.1，与 control socket "文件系统权限是唯一闸门"的定位对齐。POST 注入能操纵 agent 执行工具，是真实攻击面——若未来绑 0.0.0.0，必须先加 bearer token（Phase 2 议题）。

### 6.4 不在本设计范围（明确排除）

- WebSocket 传输（SSE 足够，见 3.2 设计原则）
- 多用户/鉴权/对外网暴露
- 现有 dashboard 监控页的行为改造（聊天页是增量路由）
- 外部 Visualizer（`clawcodex-dev viz`）集成——保持 `event_tailer.py` 的零耦合原则

---

## 附录 A：验收标准（Phase 1）

1. dsh（非流式）与 opencode（流式）各跑一个 issue，`/chat` 页均能看到**逐字**输出；
2. 发送一条 follow-up → 收到 `FollowupQueued` 帧 → 下一轮 prompt 包含该消息 → agent 后续输出可观察到其影响；
3. pause / resume / stop 按钮生效并有状态反馈；
4. 页面中途打开/刷新：历史完整渲染，仅当前 turn 已输出部分受 Phase 1 限制（Phase 2 消除）；
5. 全部新增单测通过；广播路径注入异常不外抛（agent 运行不受影响）。

## 附录 B：改动文件清单（汇总）

```
src/orchestratord/spi/degradation.py          [新] DegradingSession / DegradingBackend
src/orchestratord/backend_registry.py         [改] discover_backends 出口统一包装
src/orchestratord/agent_runner.py             [改] lazy Clawcodex 包装点；followup prompt 拼接；
                                                   TurnComplete/SessionComplete 补广播
src/orchestratord/runner_utils.py             [改] followup 命令分支 + FollowupQueued 帧
src/orchestratord/session_state.py            [改] _pending_followups 字段
src/orchestratord/control_socket.py           [改] ControlCmd 加 "followup"
src/orchestratord/chat_gateway.py             [新] UDS 桥 + 订阅分发 + 历史直读 + 截断
src/orchestratord/cli/dashboard.py            [改] /chat 路由 + SSE/POST 处理器 +
                                                   ChatGateway 生命周期挂载
src/orchestratord/event_tailer.py             [改] 新增不经过共享队列的历史直读方法

tests/test_spi_degradation.py                 [新]
tests/test_followup_injection.py              [新]
tests/test_chat_gateway_bridge.py             [新]
tests/manual_e2e_chat_stream.py               [新，默认 skip]
```
