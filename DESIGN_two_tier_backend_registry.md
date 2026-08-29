# 两层 Backend Registry 方案 — 设计文档

> **状态：** 临时设计稿，待评审。
> **目标：** 把 `backend_registry.py` 从单层 entry-point 加载升级为"描述符层 + 实现层"两层；让每个 backend 的元数据（family / 协议 / 默认 CLI / 环境前缀 / display name / model discovery）集中在单一 `BackendDescriptor` 数据结构声明，并让 drift 守护**自动**从描述符生成 EXPECTED 替代手工维护。
> **参考：** multica `server/pkg/agent/builtin_runtimes.go:18-70`（`BuiltinRuntime` 描述符）、`builtin_runtimes.go:154-159`（`ResolveBackend` 两层入口）。
> **不解决：** 第三方 backend 动态发现（仍走 Python entry_points；不动）。

---

## 0. 背景与现状

### 0.1 现状

`src/orchestratord/backend_registry.py` 仅 78 行，做两件事：

1. `discover_backends()` (`:23-46`) — `importlib.metadata.entry_points(group="orchestratord.backends")` → `ep.load()` → `DegradingBackend(ep.load()())` → dict
2. `_classify_family()` (`:67-77`) — 用 capability 位猜 family：`takeover → InProcess` / `streaming_deltas+interrupt+approval_hooks → SdkProcess` / 仅 `streaming_deltas → Protocol` / 仅 `resumable → Cli` / 否则 `Unknown`

drift 守护在 `tests/test_capability_drift.py:42-83` 手工维护 `EXPECTED: dict[str, dict[str, object]]`：

```python
EXPECTED = {
    "clawcodex": {"family": "InProcess", "bits": {...}},
    "codex":     {"family": "Cli",        "bits_cli": {...}, "bits_as": {...}},
    "dsh":       {"family": "Cli",        "bits": {...}},
    ...
}
```

每个 backend 在自己的 `backend.py` 顶部 docstring 写 `Family:` 和 `Capabilities:` 行，被 `:119-142` 的正则解析。

### 0.2 痛点

1. **EXPECTED 与 docstring 双写**：每次 backend 改位都要同步改两处 — `tests/test_capability_drift.py:42-83` 与 `backends/.../backend.py` docstring。ADR-001 §4.7 自承"the dict is the single source of truth"但实际上分散在两处。
2. **family 启发式脆弱**：`backend_registry.py:67-77` 用位猜 family；dsh 自承"故意标为 Cli 因为它的位不匹配 SdkProcess 启发式"（`test_capability_drift.py:67-74` 注释）。这是启发式与现实脱节的明确信号。
3. **CLI 二进制名散落**：`backends/.../backend.py` 各处硬编码 `subprocess.Popen(["clawcodex-dev", ...])`，与 `_backend_cli_registry.py`（`DESIGN_backend_cli_test_guard.md` 方案 A）尚未联动。
4. **runtime 身份 vs 协议家族不区分**：multica 有 `BuiltinRuntime` 把这两层分开，orchestratord 把"clawcodex-dev 这个 CLI"和"clawcodex 的 SPI backend"耦合在同一对象。

### 0.3 方案总览

| # | 子方案 | 范围 | 解决 |
|---|---|---|---|
| A | `BackendDescriptor` 数据类 | 新增 `src/orchestratord/spi/backend_descriptor.py` | 单一声明位 |
| B | entry-point 双组：描述符 + 实现 | 各 backend `pyproject.toml` + `backend_registry.py` | 把"声明"与"构造"解耦 |
| C | `resolve_backend(identifier, cfg)` 单入口 | `backend_registry.py` 新增 | runtime 身份 ↔ 协议家族统一路由 |
| D | drift 守护自动从描述符生成 EXPECTED | `tests/test_capability_drift.py` 重构 | 消灭双写 |

---

## 1. 方案 A — `BackendDescriptor` 数据类

### 1.1 目标

把每个 backend 的元数据集中到一个 frozen dataclass，**没有行为**（无方法、无 I/O）。行为在实现类上；元数据在描述符上。

### 1.2 设计

```python
# src/orchestratord/spi/backend_descriptor.py (新)
"""BackendDescriptor — 后端的声明式元数据。

每个 backend 包通过 entry_point ``orchestratord.backend_descriptors``
注册一个或多个 BackendDescriptor。每个 descriptor 描述一个具体的
runtime 身份（如 clawcodex-dev 这一可执行）以及它所属的协议家族。

与 AgentBackend (spi/backend.py) 的区别：
- AgentBackend 是行为（capabilities / create_session / dispose）
- BackendDescriptor 是声明（name / family / cli / env_prefix / model_discovery）

参考 multica BuiltinRuntime (server/pkg/agent/builtin_runtimes.go:18-70)。
"""

from __future__ import annotations
import enum
from dataclasses import dataclass, field
from typing import Literal

class BackendFamily(enum.Enum):
    """协议家族 — 比 _classify_family 启发式更可读、显式可声明。"""
    IN_PROCESS = "InProcess"
    SDK_PROCESS = "SdkProcess"
    PROTOCOL = "Protocol"
    CLI = "Cli"

@dataclass(frozen=True)
class BackendDescriptor:
    """一个后端 runtime 的声明式元数据。"""

    name: str                              # 全局唯一，如 "clawcodex-dev"
    display_name: str                      # 用户可见名，如 "Claw Codex"
    family: BackendFamily                  # 协议家族（取代 _classify_family 启发式）
    backend_package: str                   # 拥有此 runtime 的 orchestratord-* 包
    capabilities: frozenset[str]           # 声明的 capability 位名（不含 None 默认）
    cli_command: str | None = None         # 若走 CLI：subprocess.Popen 第一参数
    cli_args_probe: tuple[str, ...] = ()   # 启动探针参数（如 ["app-server", "--help"]）
    env_prefix: str | None = None          # 配置前缀（如 "CLAWCODEX_"）
    launch_header: str | None = None       # 启动 banner（日志用）
    model_discovery: Literal["static", "probe", "user"] = "user"
    """如何获取模型列表：
    - static — 硬编码于 descriptor
    - probe — 启动时调 CLI/API 探针
    - user — 用户通过 env / spec 提供
    """
    extra_metadata: dict[str, str] = field(default_factory=dict)
```

### 1.3 验收

- 模块导入无副作用
- `BackendDescriptor(name="clawcodex-dev", ...)` 不可变（frozen=True）

---

## 2. 方案 B — entry-point 双组：描述符 + 实现

### 2.1 目标

把当前单组 `orchestratord.backends` 拆为两组：

| entry-point 组 | 载荷类型 | 何时加载 |
|---|---|---|
| `orchestratord.backend_descriptors` | `BackendDescriptor` 实例（或工厂） | 启动时一次 |
| `orchestratord.backends` | `AgentBackend` 类 | 首次 `resolve_backend()` 时懒加载 |

### 2.2 每个 backend 包的改动

以 clawcodex 为例，`backends/orchestratord-clawcodex/pyproject.toml`：

```toml
[project.entry-points."orchestratord.backends"]
ClawcodexBackend = "orchestratord_clawcodex.backend:ClawcodexBackend"

[project.entry-points."orchestratord.backend_descriptors"]
clawcodex-dev = "orchestratord_clawcodex.descriptor:CLAWCODEX_DEV_DESCRIPTOR"
```

`backends/orchestratord-clawcodex/src/orchestratord_clawcodex/descriptor.py`（新）：

```python
from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

CLAWCODEX_DEV_DESCRIPTOR = BackendDescriptor(
    name="clawcodex-dev",
    display_name="Claw Codex",
    family=BackendFamily.IN_PROCESS,
    backend_package="orchestratord-clawcodex",
    capabilities=frozenset({
        "streaming_deltas", "approval_hooks", "cost_reporting",
        "tool_filtering", "takeover", "goal_mode",
    }),
    cli_command=None,           # InProcess 无外部 CLI
    env_prefix="CLAWCODEX_",
    launch_header="Claw Codex agent (in-process SDK)",
    model_discovery="probe",
)
```

### 2.3 改动清单

- 各 backend 包加 `descriptor.py`（5 个新文件）
- 各 backend 包 `pyproject.toml` 加 `orchestratord.backend_descriptors` entry-point（5 处改）
- `backends/orchestratord-clawcodex/src/orchestratord_clawcodex/backend.py` 顶部 docstring 中 `Family:` / `Capabilities:` 行**移除**（信息已迁移至 descriptor.py），改为引用

### 2.4 验收

- 5 个 backend 各自有 `descriptor.py` 且 `BackendDescriptor` 字段有效
- `pyproject.toml` 两组 entry-points 注册完整
- 现有 `discover_backends()` 仍返回原 backend 实例（向后兼容）

---

## 3. 方案 C — `resolve_backend()` 单入口

### 3.1 目标

`backend_registry.py` 新增 `resolve_backend(identifier, cfg=None) → AgentBackend`，把所有"找谁"与"怎么构造"集中到一个函数。

### 3.2 设计

```python
# src/orchestratord/backend_registry.py — 新增
def discover_descriptors() -> dict[str, BackendDescriptor]:
    """Discover all registered BackendDescriptors via entry_points.

    Returns ``{descriptor.name: descriptor}``。与 discover_backends()
    不同：descriptor 加载永不失败（无 I/O），且始终返回全部声明。
    """
    result: dict[str, BackendDescriptor] = {}
    try:
        eps = entry_points(group="orchestratord.backend_descriptors")
    except TypeError:
        eps = entry_points().get("orchestratord.backend_descriptors", [])
    for ep in eps:
        try:
            desc = ep.load()
            if isinstance(desc, BackendDescriptor):
                result[desc.name] = desc
            else:
                logger.warning("descriptor %s did not load as BackendDescriptor", ep.name)
        except Exception as exc:
            logger.warning("failed to load descriptor %s: %s", ep.name, exc)
    return result


def resolve_backend(
    identifier: str,
    *,
    cfg: BackendConfig | None = None,
) -> AgentBackend:
    """Resolve a backend by descriptor name (e.g. "clawcodex-dev").

    单一生产入口。流程：
    1. 查 descriptor 表 → 拿到 family / backend_package / cli_command
    2. 查 entry_points group="orchestratord.backends" → 拿到实现类
    3. 用 descriptor.capabilities 校验实现类的 capabilities() 返回值一致
    4. 用 DegradingBackend 包装返回

    Raises:
        BackendNotFoundError — identifier 未注册
        BackendMismatchError — 实现类 capability 与 descriptor 不一致
    """
    desc = discover_descriptors().get(identifier)
    if desc is None:
        raise BackendNotFoundError(identifier)

    impl_cls = _resolve_implementation_class(desc.backend_package)
    if impl_cls is None:
        raise BackendNotFoundError(f"{identifier} → {desc.backend_package} 未注册实现")

    backend = impl_cls()
    actual_caps = backend.capabilities()
    # 校验 desc.capabilities == {f for f in caps fields if getattr(caps, f)}
    return DegradingBackend(backend)


def _resolve_implementation_class(package_name: str) -> type | None:
    """从 entry_points 表里找 backend_package 匹配的实现类。"""
    try:
        eps = entry_points(group="orchestratord.backends")
    except TypeError:
        eps = entry_points().get("orchestratord.backends", [])
    for ep in eps:
        try:
            cls = ep.load()
            module = cls.__module__.split(".")[0]
            if module == package_name.replace("-", "_"):
                return cls
        except Exception:
            continue
    return None


class BackendNotFoundError(LookupError): ...
class BackendMismatchError(RuntimeError): ...
```

### 3.3 `_classify_family` 启发式退役

`backend_registry.py:67-77` 的 `_classify_family` 在 `list_backends()` 调用。新设计直接用 `descriptor.family.name`，删除启发式。

```python
def list_backends() -> list[dict[str, str]]:
    """列出所有 backend — 用 descriptor 而非 capability 启发式。"""
    descriptors = discover_descriptors()
    return sorted(
        [
            {
                "name": d.name,
                "display_name": d.display_name,
                "family": d.family.value,
                "backend_package": d.backend_package,
            }
            for d in descriptors.values()
        ],
        key=lambda b: b["name"],
    )
```

### 3.4 改动清单

- `src/orchestratord/backend_registry.py` — 重写为两层；新增 `discover_descriptors()` / `resolve_backend()`；删除 `_classify_family`
- `src/orchestratord/spi/backend_descriptor.py` — 新建（方案 A）

### 3.5 验收

- `resolve_backend("clawcodex-dev")` 返回 `ClawcodexBackend` 实例（被 `DegradingBackend` 包）
- `resolve_backend("nonexistent")` 抛 `BackendNotFoundError`
- `list_backends()` 返回 5 项，family 字段 = descriptor 中显式声明（无启发式）
- 现有 `discover_backends()` 保留为 deprecated alias，调用方迁移至 `resolve_backend()` 在后续 PR 完成

---

## 4. 方案 D — drift 守护自动从描述符生成 EXPECTED

### 4.1 目标

消除手工维护的 `EXPECTED: dict`。drift 守护改为**比较**"descriptor 声明的 capabilities"与"实际 backend 实例 capabilities() 的输出"。

### 4.2 设计

```python
# tests/test_capability_drift.py — 重构
def _load_descriptors() -> dict[str, BackendDescriptor]:
    """通过 entry_points 加载所有 BackendDescriptor。"""
    try:
        eps = md.entry_points(group="orchestratord.backend_descriptors")
    except Exception:
        return {}
    result = {}
    for ep in eps:
        try:
            desc = ep.load()
            if isinstance(desc, BackendDescriptor):
                result[desc.name] = desc
        except Exception:
            continue
    return result


def _load_backend_for_descriptor(desc: BackendDescriptor):
    """根据 descriptor.backend_package 找到对应 AgentBackend 实现类。"""
    try:
        eps = md.entry_points(group="orchestratord.backends")
    except Exception:
        return None
    expected_module = desc.backend_package.replace("-", "_")
    for ep in eps:
        try:
            cls = ep.load()
            if cls.__module__.startswith(expected_module + "."):
                return cls()
        except Exception:
            continue
    return None


@pytest.mark.parametrize("descriptor_name", sorted(_load_descriptors()))
def test_capability_bit_set_matches_descriptor(descriptor_name, discovered_backends):
    """capabilities() 必须等于 descriptor.capabilities。"""
    descriptors = _load_descriptors()
    desc = descriptors[descriptor_name]
    if descriptor_name not in discovered_backends:
        pytest.skip(f"backend for {descriptor_name!r} not installed")
    backend = discovered_backends[descriptor_name]
    caps = backend.capabilities()
    actual = {name for name in caps.__dataclass_fields__ if getattr(caps, name)}
    assert actual == set(desc.capabilities), (
        f"{descriptor_name}: bit set drifted: "
        f"descriptor={sorted(desc.capabilities)} actual={sorted(actual)}"
    )


def test_every_descriptor_has_an_implementation():
    """Sanity: 每个 descriptor 必须能在 orchestrator 注册的 backend 表里找到实现。"""
    descriptors = _load_descriptors()
    backends = _load_backends()
    missing = set(descriptors) - set(backends)
    assert not missing, f"descriptors without backends: {sorted(missing)}"


def test_every_backend_has_a_descriptor():
    """反向 sanity: 每个被注册的 backend 必须有对应 descriptor。"""
    descriptors = _load_descriptors()
    backends = _load_backends()
    missing = set(backends) - set(descriptors)
    assert not missing, f"backends without descriptors: {sorted(missing)}"
```

### 4.3 移除的部分

- `EXPECTED: dict`（`test_capability_drift.py:42-83`）— 整段删除
- `_extract_docstring_lines()`（`test_capability_drift.py:119-142`）— 不再解析 docstring
- `CAPABILITY_FIELD_NAMES`（`test_capability_drift.py:85-95`）— 从 `BackendCapabilities.__dataclass_fields__` 自动取
- `test_docstring_declares_family_and_capabilities_lines`（`test_capability_drift.py:191-209`）— 删除（docstring 不再是契约载体）
- 各 backend `backend.py` 顶部 docstring 的 `Family:` / `Capabilities:` 行 — 移除

### 4.4 改动清单

- `tests/test_capability_drift.py` — 整段重写
- 5 个 backend 包的 `backend.py` 顶部 docstring — 移除 `Family:` / `Capabilities:` 行

### 4.5 验收

- `pytest tests/test_capability_drift.py` 全过
- 临时改 `CLAWCODEX_DEV_DESCRIPTOR.capabilities`（去掉 `takeover`）— `test_capability_bit_set_matches_descriptor[clawcodex-dev]` 失败
- `test_capability_drift_map_is_complete` 不再存在（被 `test_every_*_has_*` 替代）

---

## 5. 完整改动清单

| 文件 | 改动 | 行数估计 |
|---|---|---|
| `src/orchestratord/spi/backend_descriptor.py` | 新建 — `BackendDescriptor` + `BackendFamily` enum | ~80 |
| `src/orchestratord/backend_registry.py` | 重写为两层；新增 `resolve_backend()`；删 `_classify_family` | +60 / -15 |
| `backends/orchestratord-clawcodex/.../descriptor.py` | 新建 | ~20 |
| `backends/orchestratord-clawcodex/pyproject.toml` | 加 descriptor entry-point | +3 |
| `backends/orchestratord-clawcodex/.../backend.py` 顶部 | 移除 docstring 行 | -3 |
| `backends/orchestratord-codex/.../descriptor.py` | 新建 | ~25 |
| `backends/orchestratord-codex/pyproject.toml` | 同上 | +3 |
| `backends/orchestratord-codex/.../backend.py` 顶部 | 移除 docstring 行 | -3 |
| `backends/orchestratord-dsh/.../descriptor.py` | 新建 | ~20 |
| `backends/orchestratord-dsh/pyproject.toml` | 同上 | +3 |
| `backends/orchestratord-dsh/.../backend.py` 顶部 | 移除 docstring 行 | -3 |
| `backends/orchestratord-hermes/.../descriptor.py` | 新建 | ~20 |
| `backends/orchestratord-hermes/pyproject.toml` | 同上 | +3 |
| `backends/orchestratord-hermes/.../backend.py` 顶部 | 移除 docstring 行 | -3 |
| `backends/orchestratord-opencode/.../descriptor.py` | 新建 | ~20 |
| `backends/orchestratord-opencode/pyproject.toml` | 同上 | +3 |
| `backends/orchestratord-opencode/.../backend.py` 顶部 | 移除 docstring 行 | -3 |
| `tests/test_capability_drift.py` | 整段重写 | -130 / +90 |
| **新测试** `tests/test_resolve_backend.py` | resolve_backend 入口单测 | +80 |
| **新测试** `tests/test_backend_descriptor_invariants.py` | 描述符字段校验 | +50 |

总计新增 9 文件 + 改 14 文件；代码净增 ~340 行。

---

## 6. 关键代码预览

### 6.1 BackendDescriptor 完整

```python
@dataclass(frozen=True)
class BackendDescriptor:
    name: str
    display_name: str
    family: BackendFamily
    backend_package: str
    capabilities: frozenset[str]
    cli_command: str | None = None
    cli_args_probe: tuple[str, ...] = ()
    env_prefix: str | None = None
    launch_header: str | None = None
    model_discovery: Literal["static", "probe", "user"] = "user"
    extra_metadata: dict[str, str] = field(default_factory=dict)
```

### 6.2 resolve_backend 主路径

```python
def resolve_backend(identifier: str, *, cfg=None) -> AgentBackend:
    descriptors = discover_descriptors()
    desc = descriptors.get(identifier)
    if desc is None:
        raise BackendNotFoundError(identifier)

    impl_cls = _resolve_implementation_class(desc.backend_package)
    if impl_cls is None:
        raise BackendNotFoundError(
            f"{identifier} → {desc.backend_package} 未注册实现"
        )

    backend = impl_cls()
    _validate_capabilities(desc, backend)
    return DegradingBackend(backend)
```

### 6.3 drift 守护新形态（核心）

```python
def test_capability_bit_set_matches_descriptor(descriptor_name, discovered_backends):
    desc = _load_descriptors()[descriptor_name]
    if descriptor_name not in discovered_backends:
        pytest.skip(...)
    backend = discovered_backends[descriptor_name]
    caps = backend.capabilities()
    actual = {n for n in caps.__dataclass_fields__ if getattr(caps, n)}
    assert actual == set(desc.capabilities), (
        f"{descriptor_name}: descriptor={sorted(desc.capabilities)} "
        f"actual={sorted(actual)}"
    )
```

---

## 7. 验收标准（Verification）

- [ ] **`BackendDescriptor` 单测**：字段验证、frozen 性、enum 值
- [ ] **5 个 backend 各自有 `descriptor.py`**：单元测试覆盖 5 个描述符字段
- [ ] **`resolve_backend()` 单测**：
  - `resolve_backend("clawcodex-dev")` 返回正确实例
  - `resolve_backend("nonexistent")` 抛 `BackendNotFoundError`
  - descriptor 与实现 capability 不一致时抛 `BackendMismatchError`
- [ ] **`list_backends()` 行为不变**：返回 5 项，family 字段值正确（InProcess / Cli / SdkProcess / Protocol）
- [ ] **`test_capability_drift.py` 全过**：旧 EXPECTED 删除；新测试覆盖 descriptor ↔ impl 双向一致性
- [ ] **CI 守护**：临时改一个 descriptor 的 `capabilities` 字段，drift 测试失败
- [ ] **现有测试套不破**：94 个 test 文件全部仍 PASS
- [ ] **`_classify_family` 删除**：grep 0 命中（仅在新代码路径出现）
- [ ] **`discover_backends()` 保留为 deprecated**：调用方迁移至 `resolve_backend()` 在后续 PR 完成
- [ ] **ADR-004 起草**：本设计的决议形成 `ADR-004-two-tier-backend-registry.md`，含本设计 §1.2 字段 + §2.2 entry-points 表 + 决策"以 descriptor 为单一真源"

---

## 8. 范围外（Out of Scope）

1. **descriptor 热加载**：本次不实现运行时新增 descriptor（仍需 Python 重启）
2. **descriptor → 配置 schema 校验**：不引入 pydantic 模型；dataclass 够用
3. **descriptor 版本/能力演进**：capabilities 是 frozenset；未来若需要"capability v1/v2"另议
4. **第三方 backend descriptor 自动发现**：仍需 entry_points 显式注册；不引入扫描 `backends/` 目录
5. **descriptor UI**：`orchestratord backends list` 输出格式不变

---

## 9. 后续（Future Work）

- 与 `DESIGN_backend_cli_test_guard.md` 联动：把 `_backend_cli_registry.KNOWN_BACKEND_CLIS` 的内容挪到 descriptor（descriptor.cli_command 已是单一真源）
- 与 `DESIGN_graded_timeouts_and_resume.md` 联动：`BackendDescriptor.capabilities` 加 `resume_detection` 位（来自 §2.4）
- descriptor 加 `model_list: tuple[str, ...] | None` 字段 — 当 `model_discovery="static"` 时填
- 在 `orchestratord backends show <name>` CLI 命令里输出完整 descriptor（YAML 格式），方便排查

---

## 10. 参考资料

- multica `builtin_runtimes.go:18-70` — `BuiltinRuntime` 描述符
- multica `builtin_runtimes.go:154-159` — `ResolveBackend()` 单入口
- orchestratord 现有 registry：`src/orchestratord/backend_registry.py`
- orchestratord drift 守护：`tests/test_capability_drift.py`
- ADR-001：[Backend Hardening §4](ADR-001-backends-hardening.md)
- ADR-002：[Goal Mode](ADR-002-goal-mode.md)
- `DESIGN_backend_cli_test_guard.md` — CLI 清单单例
- `DESIGN_graded_timeouts_and_resume.md` — Cap10 位扩展
- `DESIGN_agent_callable_skills.md` — Skill 系统（§10 联动：`BackendDescriptor.skills` 字段）