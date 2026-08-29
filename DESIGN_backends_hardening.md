# 后端硬化方案 — 设计文档

> **状态：** 临时设计稿，待评审。
> **目标：** 把当前 5 个后端的能力一致性、可观测性、可信度提到生产可用级别。
> **不解决：** 引入新后端、SPI 协议扩展、核心编排逻辑修改。

---

## 0. 背景与现状摘要

| 后端 | 能力位 (n/8) | 事件流完备度 | 关键缺陷 |
|---|---|---|---|
| clawcodex | 6/8 | 完整（含 TEXT_DELTA / TOOL_CALL / TOOL_RESULT / PHASE_COMPLETE） | `interrupt()` 是 no-op |
| codex (现役) | 2/8 | 仅 TEXT + 终态事件 | 高能力 `CodexAppServerSession` 未接线 |
| dsh | 2/8 | 缺 ERROR 分支 | `cost_reporting` 误报 False，错误吞掉 |
| hermes | 2/8 | 仅 TEXT + 终态事件 | 强制 `--yolo` 导致无审批钩子 |
| opencode | 3/8 | 仅 TEXT + 终态事件 | SSE 未翻译，能力位虚标 |

本设计文档覆盖 4 个方案：

| # | 方案 | 范围 | 预计提升 |
|---|---|---|---|
| 1 | codex 接入 AppServer 会话 | backends/orchestratord-codex | codex 2/8 → 4/8 |
| 2 | opencode SSE 真正翻译 | backends/orchestratord-opencode | opencode 事件流 1/8 → 5/8，能力位与实现对齐 |
| 3 | dsh 加 ERROR + 修正 cost_reporting | backends/orchestratord-dsh | dsh 事件流 7/8 → 8/8，能力位 2/8 → 3/8 |
| 4 | CI 能力漂移检测器 | tests/ + 核心脚本 | 全后端长期稳定性 |

---

## 1. 方案 A — 接入 `CodexAppServerSession`

### 1.1 目标

让 `orchestratord-codex` 实际拥有 `streaming_deltas + interrupt + approval_hooks + parallel_sessions` 四个能力位（4/8），对应家族从 `Cli` 升级为 `SdkProcess`。已存在的 `CodexAppServerSession`（`backends/orchestratord-codex/src/orchestratord_codex/app_server_session.py`）保留并启用。

### 1.2 设计原则

- **能力探测优先于硬编码配置**：运行时探测 `codex app-server --version` 是否存在且 ≥ 某最低版本；不存在则降级到 `codex exec --json`（现有 `CodexSession`）。这样旧版本 codex 用户不受影响。
- **不引入新的全局状态**：探测结果只存在 `CodexBackend` 进程内的 `_runtime` 字段。
- **保留两条路径**：用户可选 `--backend codex` 走 Cli 或 `--backend codex-as` 走 AppServer；探测为默认，仅当显式配置 `--prefer {cli,as}` 时覆盖。

### 1.3 改动清单

| 文件 | 改动 |
|---|---|
| `backends/orchestratord-codex/src/orchestratord_codex/backend.py` | `_detect_runtime()`：探测 `codex app-server --help` 是否 0 退出。新增 `_runtime: Literal["cli", "as"]` 字段。`create_session()` 按 `_runtime` 选择 `CodexSession` 或 `CodexAppServerSession`。`capabilities()` 返回**探测后**真实能力位（用 AS 时返回 4/8，用 CLI 时返回 2/8）。 |
| `backends/orchestratord-codex/src/orchestratord_codex/__init__.py` | 把 `CodexAppServerSession` 提到模块顶层，不再藏在子模块里。 |
| `backends/orchestratord-codex/src/orchestratord_codex/app_server_session.py` | 现有代码保留；补 `capabilities` 字段（已在 33 行），无逻辑改动。 |

### 1.4 关键代码改动预览

```python
# backend.py — 新增探测
def _detect_runtime() -> Literal["cli", "as"]:
    try:
        proc = asyncio.run(_probe(["codex", "app-server", "--help"]))
        return "as" if proc.returncode == 0 else "cli"
    except FileNotFoundError:
        return "cli"

class CodexBackend:
    name = "codex"
    display_name = "Codex"

    def __init__(self) -> None:
        self._runtime: Literal["cli", "as"] = _detect_runtime()
        self._sessions: list[AgentSession] = []

    def capabilities(self) -> BackendCapabilities:
        if self._runtime == "as":
            return BackendCapabilities(
                streaming_deltas=True,
                resumable=False,
                interrupt=True,
                approval_hooks=True,
                parallel_sessions=True,
                cost_reporting=False,
                tool_filtering=False,
                takeover=False,
            )
        return BackendCapabilities(
            streaming_deltas=False, resumable=True,
            interrupt=False, approval_hooks=False,
            parallel_sessions=True, cost_reporting=False,
            tool_filtering=False, takeover=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        cls = CodexAppServerSession if self._runtime == "as" else CodexSession
        s = cls(spec)
        self._sessions.append(s)
        return s
```

> 注：`capabilities()` 在家族分类启发式（`backend_registry.py:64`）下：`_runtime="as"` 时 `takeover=False && interrupt+approval_hooks+streaming_deltas` 三者齐全 → 正确分类为 `SdkProcess`。

### 1.5 失败模式与回退

| 情况 | 行为 |
|---|---|
| `codex` 二进制不存在 | `_detect_runtime` 抛 `FileNotFoundError` → 落到 `cli` 分支（如果 CLI 也不存在，最终 `create_session` 抛清晰错误） |
| `app-server` 子命令缺失（旧 codex） | `_probe` 非 0 退出 → 落到 `cli` 分支 |
| WorkerManager 启动后 `codex app-server` 进程立即挂 | `CodexAppServerSession.send()` 已捕获 `Exception` 转译为 `ERROR` 事件（`app_server_session.py:100-107`），无新增工作 |
| 用户显式 `--prefer cli` | 配置覆盖探测，行为等价于改前 |

### 1.6 测试

新增 `tests/test_orchestrator_codex_runtime_probe.py`：

1. **单元**：mock `asyncio.create_subprocess_exec`，断言：
   - 返回 0 → `_runtime == "as"`，`capabilities()` 含 4 个 True
   - 返回非 0 → `_runtime == "cli"`，`capabilities()` 仅 2 个 True
2. **契约**：从 `backend_registry.discover_backends()` 拿到的对象对同一 `codex` 安装返回的 `capabilities()` 集合应当**稳定**（多次调用一致）
3. **E2E**（`tests/manual_e2e_*` 模式，**默认 skip**，需要真实 `codex` 二进制）：
   - `manual_e2e_codex_as_session.py` — 起一次 send → 收 `TEXT_DELTA`/`TURN_COMPLETE`/`SESSION_COMPLETE`
   - `manual_e2e_codex_as_interrupt.py` — 触发 `interrupt()` 验证 WorkerManager `session_cancel` 路径

### 1.7 风险

- 启动期探测会增加 `orchestratord backends list` 的耗时（一次 `codex app-server --help` 调用，约 50–200ms）。可接受，但记入 ADR。
- 探测只判断"子命令是否存在"，不判断能力是否真在 runtime 上工作；如果某用户装了残缺版 codex，会得到 AS 能力位却运行时报错 → 已通过方案 A 第 1.5 节的 ERROR 事件兜底。

---

## 2. 方案 B — opencode SSE 真正翻译

### 2.1 目标

让 `orchestratord-opencode` 的事件流从只发 `TEXT` 升级为发出 `TEXT_DELTA / TOOL_CALL / TOOL_RESULT`，并让 `capabilities()` 声明的 `streaming_deltas` 与 `approval_hooks` 落到真实实现上。

### 2.2 现状复盘

`session.py:54-98` 用 `opencode serve --port 0 --pure` 起进程，**但通信用的是 `client.post("/v1/chat")` 收整个 response**（`:108-120`）——把 HTTP 一次性响应当成 TEXT 发出。这与 `streaming_deltas=True` 的声明不一致，能力位虚标。

需要真正用 `opencode serve` 的 SSE 流。

### 2.3 opencode serve 协议假设

> 待方案落地时通过抓包验证；目前依据 README 与源码推测。

- `POST /v1/chat`，`Accept: text/event-stream` → SSE 流
- 事件类型（推测，待验证）：
  - `event: message.delta` — `data: {"text": "..."}`
  - `event: tool.call` — `data: {"call_id": "...", "name": "...", "arguments": {...}}`
  - `event: tool.result` — `data: {"call_id": "...", "output": ...}`
  - `event: approval.request` — `data: {"request_id": "...", "tool_name": "...", "arguments": {...}}`
  - `event: turn.complete` — `data: {"reason": "..."}`

落地前需要 `opencode serve` 协议反向工程；如果事件名不对，需要做协议适配层。

### 2.4 改动清单

| 文件 | 改动 |
|---|---|
| `backends/orchestratord-opencode/src/orchestratord_opencode/session.py` | `send()` 改为 `client.stream("POST", "/v1/chat", ...)`；解析 SSE 帧，按事件名分派到 `_translate_event`；approval 请求缓存进 `_pending_approvals: dict[request_id, ApprovalRequest]`，等待 `approve()` 调用。 |
| 同上 | `_ensure_server` 增加端口发现超时保护（当前 30 次轮询无显式超时 → 改为 `asyncio.wait_for(total=10s)`）。 |
| 同上 | 移除 `--pure` 参数（纯文本模式可能不支持 SSE）。如确为协议要求则保留并在 docstring 解释。 |
| `backends/orchestratord-opencode/src/orchestratord_opencode/backend.py` | `capabilities()` 保持 `streaming_deltas=True, approval_hooks=True`（与新实现对齐）。 |

### 2.5 关键代码改动预览

```python
# session.py — 新增 SSE 流
async def send(self, content):
    port, client = await self._ensure_server()
    text = content if isinstance(content, str) else str(content)
    try:
        async with client.stream(
            "POST", "/v1/chat",
            json={"prompt": text, "model": self._spec.model},
            timeout=300.0,
        ) as resp:
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = json.loads(line[len("data:"):].strip())
                self._ingest_sse(payload)
    except Exception as exc:
        self._add_event(EventKind.ERROR, {"code": "opencode_error", "message": str(exc)})
    finally:
        self._add_event(EventKind.TURN_COMPLETE, {"reason": "success"})
        self._add_event(EventKind.SESSION_COMPLETE, {"reason": "success"})

def _ingest_sse(self, payload: dict):
    kind = payload.get("event")
    if kind == "message.delta":
        self._add_event(EventKind.TEXT_DELTA, {"text": payload["text"], "delta": payload["text"]})
    elif kind == "tool.call":
        self._add_event(EventKind.TOOL_CALL, {
            "call_id": payload["call_id"],
            "name": payload["name"],
            "arguments": payload["arguments"],
        })
    elif kind == "tool.result":
        self._add_event(EventKind.TOOL_RESULT, {
            "call_id": payload["call_id"],
            "ok": True,
            "output": payload["output"],
        })
    elif kind == "approval.request":
        req_id = payload["request_id"]
        self._pending_approvals[req_id] = ApprovalRequest(
            request_id=req_id,
            call_id=payload.get("call_id", ""),
            tool_name=payload["tool_name"],
            arguments=payload.get("arguments", {}),
        )

async def approve(self, request_id: str, decision: ApprovalDecision):
    if request_id not in self._pending_approvals:
        return
    req = self._pending_approvals.pop(request_id)
    await self._client.post(f"/v1/approvals/{request_id}", json={
        "decision": decision.value, "call_id": req.call_id,
    })
```

### 2.6 失败模式

| 情况 | 行为 |
|---|---|
| opencode 版本过老 | `client.stream` 仍走 HTTP/1.1 但服务端返回 `Content-Type: application/json`（非 SSE）→ `_ingest_sse` 全是 None，事件流降级为 `TEXT` + 终态。**显式 log warning 并回退 capability 位声明到 `streaming_deltas=False`** |
| SSE 中途连接断开 | `httpx` 抛 `RemoteProtocolError`，外层 except 转译为 `ERROR` 事件；`SESSION_COMPLETE.reason="error"` |
| `approval.request` 未到 `approve()` 就超时 | `_pending_approvals` 在 `close()` 时清空；core 侧的 approval policy 已有 `timeout_seconds`（`spi/approval.py:40`），由它触发取消 |

### 2.7 测试

1. **协议层 mock**：`tests/test_orchestrator_opencode_sse_translation.py`
   - mock `httpx.AsyncClient.stream` 喂三帧 SSE → 断言收到 `TEXT_DELTA × N, TOOL_CALL, TOOL_RESULT` 顺序且 seq 递增
2. **approval 流程**：mock `/v1/approvals/{id}` → 调用 `session.approve("req-1", ALLOW)` → 断言 POST body 正确
3. **E2E**：`tests/manual_e2e_opencode_sse.py` — 默认 skip，需要本地 `opencode serve`

### 2.8 风险

- **协议假设可能错**：opencode serve 的事件名未必如推测。落地时必须做一次反向工程；如不符，需要 `protocol.py` 适配层。
- **httpx `stream` API 在 0.27 前后有 breaking change**：要求 `httpx>=0.27`，需在 `pyproject.toml` 加约束（目前 `session.py` 顶层 `import httpx` 无版本固定）。
- **能力位协商可能反向**：如果协议实现不了某些能力，方案 B 的反向回退（`streaming_deltas=False`）需要在 `capabilities()` 里也动态化，而不是写死 `True`。

---

## 3. 方案 C — dsh 加 ERROR 分支 + 修正 cost_reporting

### 3.1 目标

- 让 dsh 的事件流补上 `ERROR` 分支，不再让异常被吞进 `SESSION_COMPLETE`。
- 把 `cost_reporting` 从 `False` 改成 `True`，因为 `deepseek-harness-sdk` 的 `RunResult` 实际有 token 计数（待 SDK 版本验证）。
- 让 `BackendCapabilities` 真正反映 SDK 能提供的信息。

### 3.2 改动清单

| 文件 | 改动 |
|---|---|
| `backends/orchestratord-dsh/src/orchestratord_dsh/session.py` | `_ingest_result` 增加 `ERROR` 事件分支：当 `result.finish_reason` 非 `success` 且不是已知终态时，发 `ERROR` 事件，并**仍保留** `SESSION_COMPLETE`。 |
| 同上 | `_send_sync` 用 try/except 包 `harness.run()`；`HarnessError` / `TimeoutError` 等 SDK 异常直接转 `ERROR` 事件，**不调用 `_ingest_result`**。 |
| 同上 | `_get_harness` 中 `DeepSeekHarness(config)` 失败（如 SDK 版本不兼容、模型名非法）也要转 `ERROR`。 |
| 同上 | `capabilities` 字段改为 `cost_reporting=True`，并在 docstring 说明依赖 SDK ≥ 某版本（落地时核对）。 |

### 3.3 关键代码改动预览

```python
# session.py — 错误处理补全
async def send(self, content):
    if self._closed:
        raise RuntimeError("session closed")
    text = content if isinstance(content, str) else str(content)
    try:
        result = await asyncio.to_thread(self._send_sync, text)
        self._ingest_result(result)
    except Exception as exc:                              # ← 新增
        self._add_event(EventKind.ERROR, {                # ← 新增
            "code": "dsh_error",
            "message": f"{type(exc).__name__}: {exc}",
        })
    finally:
        # 无论成败，SESSION_COMPLETE 必须发出（核心依赖它来释放资源）
        reason = "error" if self._events and self._events[-1].kind == EventKind.ERROR else "success"
        self._add_event(EventKind.SESSION_COMPLETE, {"reason": reason})

def _ingest_result(self, result):
    for event in result.events:
        translated = self._translate_event(event)
        if translated is not None:
            self._events.append(translated)
    # NEW: 当 SDK 报告非 success 但没有 ERROR 事件时，补一条
    if result.finish_reason not in (None, "success", "stop"):
        self._events.append(EventEnvelope(
            seq=self._next_seq(), timestamp=self._now(),
            kind=EventKind.ERROR,
            payload={"code": "dsh_finish", "reason": result.finish_reason},
        ))

# capabilities() — 修正
def capabilities(self) -> BackendCapabilities:
    return BackendCapabilities(
        streaming_deltas=False,
        resumable=True,
        interrupt=False,
        approval_hooks=False,
        parallel_sessions=True,
        cost_reporting=True,     # ← 改
        tool_filtering=False,
        takeover=False,
    )
```

### 3.4 失败模式

| 情况 | 行为 |
|---|---|
| SDK 抛 `HarnessError`（token 超限、模型不可达） | `send()` 外层 except 转 `ERROR`；不调 `_ingest_result` |
| SDK 返回 `RunResult.finish_reason="error"`（API 5xx） | `_ingest_result` 末尾分支转 `ERROR` |
| `_get_harness` 抛 `ValueError("unknown model")` | `send()` 外层 except 转 `ERROR`；harness 进程未启动，close() 不需操作 |
| `cost_reporting=True` 但 SDK 字段缺失 | 仍声明 True；`run()` 结果若 `usage is None`，事件流不发 COST 事件（当前 SPI 无 COST kind，方案 D 不在范围内）—— 只把 capability 位作为**未来扩展位** |

### 3.5 测试

1. `tests/test_orchestrator_dsh_error_propagation.py`
   - mock `harness.run` 抛 `RuntimeError("rate limit")` → 断言收到 `ERROR{code:"dsh_error", message:...}` 然后 `SESSION_COMPLETE{reason:...}`
   - mock `harness.run` 返回 `RunResult(events=[], finish_reason="error")` → 断言 `ERROR{code:"dsh_finish", reason:"error"}`
2. `test_orchestrator_dsh_capabilities.py`
   - 断言 `capabilities().cost_reporting is True`

### 3.6 风险

- **SDK 版本耦合**：`cost_reporting=True` 假设 SDK 有 `usage` 字段；如果 SDK 版本没有，发不出任何 token 数据，capability 位撒谎。**需要在 `_get_harness` 后探测 `result.usage` 是否非 None 才声明 True**；否则降级回 False。
- **错误处理会重复发事件**：现有代码 `send()` 末尾已发 `SESSION_COMPLETE`，外层 `finally` 再发一次会重复。**必须把现有末尾两行挪进 try 分支**，只在成功路径发；错误路径只发 ERROR，finally 只发一次 SESSION_COMPLETE。

---

## 4. 方案 D — CI 能力漂移检测器

### 4.1 目标

在 CI 里**自动**跑每个后端的 `capabilities()`，并断言：

1. 实际能力位 = 后端 docstring 中声明的能力位（"声明漂移"）
2. 实际能力位 ⊆ 实际事件流覆盖（"实现漂移"）
3. 后端家族分类（`backend_registry._classify_family`）与 docstring 中家族标注一致

任何漂移 → CI 红。

### 4.2 设计原则

- **不引入新依赖**：只用 `pytest` + `importlib.metadata.entry_points`，已存在。
- **每个后端一个测试文件**：与现有 `tests/test_orchestrator_*.py` 命名风格一致。
- **不替代人工 review**：检测器只暴露漂移，不自动修复。
- **可被本地跑**：`pytest -k capability_drift` 应能在 5 秒内跑完（不依赖真实外部二进制）。

### 4.3 改动清单

| 文件 | 改动 |
|---|---|
| `tests/test_capability_drift.py` | 新建。导入每个后端，遍历 `capabilities()`，与 docstring 中的"Family/Features"声明字符串做正则比对。 |
| `tests/conftest.py` | 增加 `backends_with_docstrings` 参数化 fixture，从 `backends/*/src/*/*.py` 收集 docstring。 |
| `.github/workflows/*.yml`（或 CI 配置） | 在 `pytest` 步骤增加 `-k capability_drift` 必跑项；不通过即 fail。 |
| `backends/*/src/*/backend.py`（每个） | docstring 中**显式**标注 `Family: <name>` 与 `Capabilities: streaming_deltas resumable ...`，给检测器作为 ground truth。 |

### 4.4 关键代码改动预览

```python
# tests/test_capability_drift.py
import importlib.metadata as md
import inspect
import re
import pytest

# 把"声明位"硬编码——这是设计的一部分：
# 后端 docstring 必须以可解析的格式写出 capability，检测器才能比对。
EXPECTED = {
    "clawcodex": {
        "family": "InProcess",
        "bits": {"streaming_deltas", "approval_hooks",
                 "cost_reporting", "tool_filtering", "takeover"},
    },
    "codex": {
        "family": "Cli",  # 现役 CLI 路径
        "bits": {"resumable", "parallel_sessions"},
    },
    "dsh": {
        "family": "Cli",  # SPI 分类与 docstring 不一致，CI 强制按 SPI
        "bits": {"resumable", "parallel_sessions", "cost_reporting"},
    },
    "hermes": {
        "family": "Cli",
        "bits": {"resumable", "parallel_sessions"},
    },
    "opencode": {
        "family": "Protocol",
        "bits": {"streaming_deltas", "approval_hooks", "parallel_sessions"},
    },
}

def _load_backends():
    eps = md.entry_points(group="orchestratord.backends")
    return {ep.name: ep.load()() for ep in eps}

@pytest.mark.parametrize("name", list(EXPECTED))
def test_capability_drift(name):
    backends = _load_backends()
    if name not in backends:
        pytest.skip(f"backend {name!r} not installed")
    backend = backends[name]
    caps = backend.capabilities()
    declared_bits = EXPECTED[name]["bits"]

    # 1. 实际位 == 声明位
    actual = {f for f in vars(caps) if getattr(caps, f)}
    assert actual == declared_bits, (
        f"{name}: declared bits {declared_bits} ≠ actual {actual}"
    )

    # 2. docstring 必须含 family 声明
    src = inspect.getsource(type(backend))
    m = re.search(r"Family:\s*(\w+)", src)
    assert m, f"{name}: docstring missing 'Family:' line"
    assert m.group(1) == EXPECTED[name]["family"], (
        f"{name}: docstring family {m.group(1)} ≠ "
        f"ground truth {EXPECTED[name]['family']}"
    )

    # 3. 与 SPI 分类启发式一致
    from orchestratord.backend_registry import _classify_family
    assert _classify_family(caps) == EXPECTED[name]["family"], (
        f"{name}: SPI classifier returns {_classify_family(caps)}"
        f" ≠ ground truth {EXPECTED[name]['family']}"
    )
```

> **dsh 故意标为 Cli**：与 SPI 分类一致；它自述 SdkProcess 的 docstring 不被 ground truth 采信——这迫使修复 dsh 的家族对齐（见 §5.1 后续行动）。

### 4.5 失败模式与报警

| 漂移类型 | 检测方式 | 报警位置 |
|---|---|---|
| 实际能力位 ≠ docstring 声明 | `actual == declared_bits` 失败 | 测试 fail 输出 diff |
| docstring 缺 `Family:` 行 | 正则 `re.search(r"Family:\s*...")` 失败 | 测试 fail |
| SPI 分类与 docstring Family 不一致 | `_classify_family(caps)` ≠ declared | 测试 fail |
| 后端未安装（可选） | `pytest.skip`；警告而非 fail | stdout warning |

### 4.6 测试

`test_capability_drift.py` 自身就是测试。要新增一个**反向**测试：

```python
def test_drift_detector_catches_intentional_mismatch():
    """把 EXPECTED 临时改成错的，断言会 fail——确保检测器不是空跑。"""
    # 用 monkeypatch 临时改 EXPECTED，断言断言失败
    ...
```

### 4.7 风险

- **`EXPECTED` 字典是单一真相源**：写错一处会让所有后端被认为漂移。必须由方案 A/B/C 完成后**人工**同步更新，并加 PR review checklist "capability matrix updated"。
- **后端不在 dev install 范围**：`pip install -e .[dev]` 当前只装 core，不装后端。CI 必须先 `pip install -e backends/*` 才能跑 drift 检测。需要在 `pyproject.toml` 的 `[project.optional-dependencies]` 加 `backends = ["orchestratord-clawcodex", ...]`，或文档化 CI 步骤。
- **方案 A 让 codex 能力位运行时变化**：drift 检测器只在后端构造时调一次 `capabilities()`，无法覆盖运行时切换（AS vs CLI）。需要在 docstring 显式说明，或在 `EXPECTED["codex"]` 里写**所有可能位的并集**并允许 `subset` 判断。

---

## 5. 横向问题与排序建议

### 5.1 必须解决的前置问题

| 问题 | 阻塞 | 处理方式 |
|---|---|---|
| 后端包默认未安装，dev/CI 跑不全 | 方案 D | 加 `pyproject.toml[dev]` 包含全部5 个后端路径；或 CI 步骤显式 `pip install -e backends/*` |
| 文档 `Family:` 行缺失 | 方案 D | 一次性改齐 5 个后端 docstring |
| `EXPECTED` ground truth 初值未定 | 方案 D | 4 个方案落地后回填 |

### 5.2 推荐落地顺序

```
[1] 修 dsh ERROR + cost_reporting（方案 C）        ← 最快，最独立
    │
[2] 接入 codex AppServer（方案 A）                ← 依赖 WorkerManager（已有）
    │
[3] opencode SSE 翻译（方案 B）                   ← 依赖协议反向工程，可能延期
    │
[4] CI 漂移检测器（方案 D）                       ← 必须最后；以前 3 步落地后的真值填 EXPECTED
```

理由：C 改动局部、影响小、可快速验证；A 改动 backend.py 但复用既有 WorkerManager；B 需要协议反向工程，最不可控；D 是收口工具，依赖前面三步的最终状态。

### 5.3 不在本设计范围（明确排除）

- SPI 协议扩展（如新增 `cost_event` 类型、`progress_event` 类型）—— 影响所有后端，单独 ADR
- clawcodex 的 `interrupt()` 实现 —— 需 clawcodex QueryRunner 先支持
- hermes 强制 `--yolo` 改造 —— 影响用户工作流
- 新增更多后端

### 5.4 验收标准

每个方案完成后必须满足：

1. 新增/修改测试全部通过
2. `pytest -k capability_drift` 通过
3. README 中的能力矩阵 + 事件流对比表已被实际值更新
4. ADR（`docs/adr/` 或根目录 `*-adr.md`）记录取舍

---

## 附录 A：能力位变化前后对比

| 后端 | 方案前 | 方案后 | 增量 |
|---|---|---|---|
| clawcodex | 6/8 | 6/8 | （不在本设计范围） |
| codex | 2/8 | **4/8** | +streaming_deltas, +interrupt, +approval_hooks |
| dsh | 2/8 | **3/8** | +cost_reporting；事件流 +ERROR |
| hermes | 2/8 | 2/8 | （不在本设计范围） |
| opencode | 3/8（虚标） | 3/8（实标） | 事件流从 1/8 → 5/8，能力位与实现对齐 |

## 附录 B：改动文件清单（汇总）

```
backends/orchestratord-codex/
  src/orchestratord_codex/backend.py        [改] _detect_runtime + 动态 capabilities
  src/orchestratord_codex/__init__.py      [改] 导出 CodexAppServerSession
  src/orchestratord_codex/app_server_session.py  [不动]

backends/orchestratord-opencode/
  src/orchestratord_opencode/session.py    [改] SSE 翻译 + approval 转发
  src/orchestratord_opencode/backend.py    [不动]

backends/orchestratord-dsh/
  src/orchestratord_dsh/session.py         [改] ERROR 分支 + cost_reporting=True

backends/orchestratord-{clawcodex,hermes}/
  src/.../backend.py                       [改] docstring 增加 Family/Capabilities 行（方案 D 需要）

tests/
  test_orchestrator_codex_runtime_probe.py [新]
  test_orchestrator_opencode_sse_translation.py  [新]
  test_orchestrator_dsh_error_propagation.py     [新]
  test_orchestrator_dsh_capabilities.py          [新]
  test_capability_drift.py                       [新]
  manual_e2e_codex_as_session.py                 [新]
  manual_e2e_codex_as_interrupt.py               [新]
  manual_e2e_opencode_sse.py                     [新]

pyproject.toml                              [改] httpx 版本约束；dev 包含全部5 个后端路径
README.md                                   [改] 更新能力矩阵与事件流对比表
ADR-001-backends-hardening.md               [新] 记录本设计取舍
```