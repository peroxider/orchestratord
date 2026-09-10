# DESIGN: PR 门禁测试（Gate Tests）设计

> 状态：v2 —— 原三项开放问题已定案（§12，2026-09-09）：G4b 纳入 issue_pr 业务链路、基线独立 commit、Windows 豁免登记
> 关联：`test_architecture.py`（豁免登记表模式）、`DESIGN_backend_cli_test_guard.md`（CLI PATH 守卫）、`pyproject.toml §9.3`（agentintegration 烟囱）

## 0. 摘要

orchestratord 既是 agent 编排器，也是编排器的自迭代开发对象：agent 提交的 PR 会被合入本仓库并再次作为运行时对外服务。因此需要一道**合入门禁（merge gate）**，回答一个问题：

> **这个 PR 之后的 orchestratord，能否被一个真实用户正常启动并完成常规操作？**

现有测试体系以进程内单元/契约测试为主（`test_serve_cli.py` 甚至显式 fake 掉 uvicorn），无法捕捉"真实子进程启动失败、运行期崩溃、模块导入断裂"这类用户面事故。本文设计一套分层门禁套件（G0–G5），核心是**真实子进程冒烟**：拉起真实 daemon 与真实 uvicorn，用确定性 stub backend 走通核心链路，并以**机器可读报告**回馈给自迭代 agent。

## 1. 背景与问题

### 1.1 风险模型

agent 提交的 PR 与人类 PR 的差异：agent 更容易引入"语法正确、测试可能全绿、但用户面已坏"的变更，典型形态包括：

| 风险形态 | 现有测试为何接不住 |
|---|---|
| 循环导入 / 顶层副作用导致 CLI 或 API 模块 import 即崩 | 单元测试只 import 被测模块；`cli/main.py` 的懒加载 dispatch（`main.py:53-84`）只有真实调用才触发 |
| `create_app()` 的 lifespan 初始化崩了（DB engine、seed、realtime broker） | `test_serve_cli.py:42-73` fake 掉 uvicorn，从不真正绑定 socket |
| alembic 迁移与 `create_schema()` 漂移（新表只进了其中一处） | `tests/api/conftest.py` 用 `create_schema` 建库，不跑迁移 |
| 子命令 parser 注册遗漏 / dispatch 分支丢失 | 无锁定测试，`--help` 从不在 CI 中逐个执行 |
| daemon 无法优雅退出（信号处理、GOODBYE drain 回归） | 现有测试不经过真实 SIGTERM |
| backend 包（`backends/*`）与本体的入口点契约断裂 | 漂移检测按需 opt-in，非强制门禁 |

### 1.2 设计目标

1. **用户面等价性**：门禁模拟的正是用户操作路径 —— 敲 CLI、起 daemon、发 HTTP 请求、跑一轮任务。
2. **fail-closed**：门禁自身崩溃、超时、环境缺失 → 一律判 FAIL，绝不静默跳过算过。环境缺失应显式豁免（§9），而不是 `pytest.skip()`。
3. **确定性**：不依赖真实 agent CLI、真实外网、真实用户 keychain；复用 `install_cli_shims` 的 stub 体系。
4. **对 agent 友好**：失败输出机器可读（JSON 报告），让自迭代 agent 能自主定位并修复。
5. **有预算**：PR 门禁总时长 ≤ 15 分钟（含 issue_pr 业务链路），超出预算的检查降级到 nightly。

## 2. 保护对象：用户面清单

门禁的"主要功能健全"以下列用户面为准（按破坏严重度排序）：

| # | 用户面 | 入口 | 破坏的典型症状 |
|---|---|---|---|
| U1 | CLI 可用 | `orchestratord.cli.main:app`（15 个子命令组：server/daemon、run、backend、app、issue、workflow、dashboard、serve、web、db、rules、workspace、peer、skills、telemetry） | import 崩溃、`--help` 非零退出 |
| U2 | daemon 启停 | `orchestratord server start` / `stop`（`cli/server.py`） | 启动即崩、卡死、SIGTERM 后不退出 |
| U3 | HTTP API | `orchestratord.api.app:create_app`（~24 个 router，uvicorn serve） | lifespan 崩溃、路由 500、OpenAPI schema 生成失败 |
| U4 | 健康探针 | `GET /api/health`（免鉴权，`api/routers/dashboard.py:75`） | 返回非 200 |
| U5 | 核心编排链路 | 提交任务 → kernel dispatch loop → backend runner → 事件/transcript 落盘 → 状态回写 | 任务永久 pending、事件丢失、状态机卡死 |
| U6 | DB | Postgres + SQLAlchemy（asyncpg 钉在 <0.30）、alembic 迁移 | 建库失败、迁移不可重放、新字段未落库 |
| U7 | backend 插件契约 | `backends/*` 经入口点被 `backend_registry` 发现 | 六个 backend 包零个可加载 |
| U8 | Web 前端 | `orchestratord web` 提供的静态资源 | 资源 404、构建产物缺失 |

## 3. 设计原则

1. **真实进程优先（Real-Process-First）**：U1–U4、U8 的检查必须拉起**真实子进程**（`subprocess.Popen` 真实的 `orchestratord` console_script），禁止 mock CLI 入口、禁止 fake uvicorn。进程内测试继续承担逻辑正确性，门禁只回答"能不能跑"。
2. **确定性 stub**：任何需要 agent CLI 的路径使用 `install_cli_shims()` 生成的 stub（复用 `tests/conftest.py` 的守卫设施），保证门禁不依赖用户机器上装了什么。
3. **豁免必须登记（No Silent Skip）**：借鉴 `test_architecture.py` 的豁免登记表模式——每个因环境无法执行的检查必须显式登记（原因 + 复活条件），登记表过期（环境其实可用）同样判 FAIL。禁止裸 `pytest.skip` 出现在门禁目录。
4. **机器可读失败报告**：门禁结束产出 `gate-report.json`（每检查项：id、verdict、耗时、stderr 摘录、定位提示），供自迭代 agent 消费；人类阅读同一报告的 markdown 渲染。
5. **有预算**：总预算 10 分钟；单检查项超时即 FAIL 并 kill 子进程（不留孤儿进程，fixture 侧 `psutil` 兜底清理）。

## 4. 门禁分层

```
G0  静态健全       compileall / 全模块 import / ruff          (~1 min)
G1  CLI 冒烟       --version + 15 个子命令 --help 真实退出码    (~1 min)
G2  迁移健全       alembic upgrade/downgrade 对拍 create_schema (~2 min)
G3  启动冒烟       真实 daemon start → /api/health → SIGTERM    (~2 min)
G4  功能冒烟       echo 端到端 + 核心 CRUD 往返 + issue_pr 链路  (~6 min)
G5  架构与契约守卫  现有守卫测试收编为门禁必选集                  (~1 min)
```

G0/G1/G5 无外部依赖，任何环境必跑；G2/G3/G4 依赖 Postgres，缺失时走豁免登记而非跳过。以下逐一给出设计。

## 5. 各层详细设计

新增目录 `tests/gate/`，统一打 `@pytest.mark.gate` marker（在 `pyproject.toml` 注册），门禁 runner 用 `-m gate` 一次拉起：

```
tests/gate/
  gate_support.py        # 豁免登记表（§9 唯一事实源）、子进程设施、日志扫描、psutil 清理
  expected_cli_subcommands.txt   # G1 子命令锁定基线
  test_g0_static.py
  test_g1_cli_smoke.py
  test_g2_migrations.py
  test_g3_startup.py
  test_g4_functional.py
  gate_runner.py         # pytest 包装：产出 gate-report.json / gate-report.md
```

注：G5 守卫不设独立模块——`gate_runner.py` 以显式 nodeid 列表（`GUARD_NODEIDS`）单独跑一轮 pytest 后与套件结果合并，层号由 `_layer_of` 从 case id 解析。

### 5.1 G0 — 静态健全（防"import 即崩"）

- **全模块强制导入**：遍历 `src/orchestratord` 全部 `.py`，逐个 `importlib.import_module`，任何 `ImportError`/顶层异常判 FAIL。这是对懒加载 dispatch 的兜底——`cli/main.py` 用函数内 import，单元测试永远不触发，门禁必须全量触发一次。六个 `backends/*/src` 包同样纳入（放行入口点契约 U7）。
- `python -m compileall src backends -q`：语法层最快的失败信号。
- `ruff check`：**落地时收窄为 `--select E9`**（语法/未定义名级错误）——实测存量违规 963 条（UP037×191、BLE001×186、F401×89、I001×65…），仓库从未强制过 ruff，零告警阈值不具备落地条件。收紧路线：按规则族分季度清零后逐批加入 `--select`（每批一次独立基线 commit，§6），直至全规则集。注意 ruff 前缀匹配陷阱：`--select E9,F7,F63,F82` 会把 F821 也纳入（F8 前缀）。
- `orchestratord --version` 打印版本且 `_version.py` 可解析。

### 5.2 G1 — CLI 冒烟（U1）

对 15 个子命令逐一真实执行 `orchestratord <sub> --help`，断言：

- 退出码 == 0；
- stderr 无 `Traceback`；
- 输出包含 usage 行。

子命令清单**锁定**在 `tests/gate/expected_cli_subcommands.txt`（模式同 `scripts/agent-cli-command-names.txt` 的 `test_agent_cli_command_lock.py`）：新增子命令必须同步更新清单，防止"加了 parser 忘了注册 dispatch"或反向"删了模块留了死分支"。另抽测 2 个只读命令的真实执行路径（`orchestratord issue list`、`orchestratord workspace list`）跑在 `isolated_tmp_repo` 式临时目录上，覆盖 argparse 之后的真实 `run()` 分支。

### 5.3 G2 — 迁移健全（U6）

在门禁专用的临时数据库 `orchestratord_gate`（每次重建）上：

1. `alembic upgrade head` 全量重放 → 成功；
2. `create_schema()` 建第二份空库 → 对拍两库表集合与列集合**完全一致**（迁移与 ORM 元数据不漂移，堵 `tests/api/conftest.py` 用 create_schema 而漏掉迁移的盲区）；
3. `alembic downgrade base` → 成功（迁移可逆性抽查，允许登记豁免"某迁移不可逆"并说明）；
4. 旧库升级：对 `upgrade head-1` 的库执行最新迁移 → 成功（模拟真实用户升级路径）。

### 5.4 G3 — 启动冒烟（U2/U3/U4）

这是本设计的核心增量。fixture 拉起**真实子进程**：

```
1. tmp 工作区 + 门禁专用测试库 DSN（环境变量注入）
2. subprocess.Popen([sys.executable, "-m", "orchestratord", "server", "start", ...])
   —— 走 console_script 等价路径，捕获 stdout/stderr 到文件
3. 轮询 http://127.0.0.1:<port>/api/health，预算 30s
4. 断言 200 且响应体含预期字段
5. 抽查 OpenAPI：GET /openapi.json → 200 且 paths 数 ≥ 锁定基线
6. SIGTERM → 进程在预算内退出且退出码 ∈ {0}
7. 全程扫描日志文件：出现 "Traceback" / "ERROR" 未在豁免清单 → FAIL
```

配套两个变体：

- **serve 变体**：`orchestratord serve`（uvicorn 路径）同样做 3–5 步，补上 fake-uvicorn 测试留下的真实 socket 盲区。**落地实测（uvicorn 0.52.4）**：`capture_signals` 在优雅关闭完成后会重放捕获到的 SIGTERM，进程以 `-15` 结束属 uvicorn 标准语义（日志含完整 "Application shutdown complete" 关闭序列）。判据放宽为：退出码 ∈ {0, -15} **且** 日志含优雅关闭完成标记 **且** 无 Traceback；
- **daemon stop 变体**：start 后立即 `server stop`，验证 PID 文件清理与不误杀（对应 #34 类缺陷的门禁化）。注意 workspace root 必须用 make_workflow 改写后的实际路径（`<tmp>/orchestratord-workspace`），传错路径会因 slug 不匹配而幂等返回 0 却没杀到 daemon。

fixture 使用 `psutil.Process.children(recursive=True)` 在 teardown 兜底 kill，保证门禁失败也不污染环境。

**平台差异（已定案）**：Windows 原生（非 WSL）路径下 SIGTERM 语义与 POSIX 不同（无 SIGTERM、`terminate()` 等价于 `TerminateProcess` 强杀、进程树需 `taskkill /T` 或 psutil 自顶向下递归终止）。G3 的"SIGTERM 后优雅退出码 0"断言在该平台上放宽为"psutil 递归终止后无存活子进程、日志无未处理异常"，且这一放宽**必须以平台条件豁免登记**（§9，`platform == "win32"` 条件项），不得写成裸的 `sys.platform` 分支跳过——登记过期或 Linux 上误用该豁免即 FAIL。WSL2（本项目主开发环境）按 POSIX 路径执行，不受此豁免影响。

### 5.5 G4 — 功能冒烟（U5，核心链路端到端）

以 **echo 应用**为最小真实载体（`applications/echo/`，无外部副作用），stub backend 提供确定性回包：

```
1. G3 的活 daemon 上，通过 CLI（orchestratord run / issue 提交）发起一个 echo 任务
2. stub backend（install_cli_shims 体系，echo 语义：原样回显输入）
3. 轮询任务状态至终态，预算 60s
4. 断言：终态 == 成功；事件序列完整（至少 dispatched → backend_finished → resolved）；
   transcript 文件存在且含 stub 回包内容
```

再叠加**API CRUD 往返**：对 projects / workspaces / issues 三个核心资源各做一次 create → read → update → list 过滤，走真实 HTTP（带鉴权 token，`ORCHESTRATORD_AUTH=1` 路径，与 `tests/api/conftest.py:30` 一致）。

### 5.5.1 G4b — issue_pr 业务链路冒烟（U5 业务面）

**已定案**：G4 必须覆盖 issue_pr 应用（`applications/issue_pr/`）的 issue→PR 业务链路——这是本项目最主要的用户工作流，仅测 echo 不足以证明"主要功能健全"。

与 echo 层的差异及处理：

```
1. fake git remote：fixtures 提供一个本地 bare 仓库充当 origin（ isolated_tmp_repo
   + git clone --bare 生成），issue_pr 的 repo_tracker / PR 回退路径全部落在本地
   文件系统，无外网、无真实 forge
2. stub backend 回包预置：clarifier 决议与执行结论走 install_cli_shims 体系的
   确定性 stub（fixed 回包脚本），绕开真实 LLM
3. 链路：CLI 提交 issue → clarifier（stub 决议）→ kernel dispatch → stub backend
   执行 → repo_tracker 在 fake remote 上产生 commit → 状态终态
4. 断言：issue 终态成功；事件序列含 issue 阶段标记；fake remote 上出现预期
   commit；审计/事件日志无未登记 ERROR
5. 预算 120s；失败时报告须摘录 clarifier 决议与 backend 回包，便于 agent 定位
```

设计约束：fake remote 与 stub 回包脚本放 `tests/gate/fixtures/issue_pr/`，与既有 `tests/contracts/stub_backend.py` 共享实现，避免两套 stub 漂移。issue_pr 链路涉及 git 操作，沿用 `isolated_tmp_repo` 的确定性 git 身份配置。

### 5.6 G5 — 架构与契约守卫（收编现有资产）

将以下已存在的守卫测试纳入门禁必选集（不改写，仅通过 `-m gate` 标注或 runner 显式 nodeid 列表收录）：

- `test_architecture.py`（机制/业务域依赖规则 + 豁免登记表）；
- `test_layer_isolation.py`；
- `test_agent_cli_command_lock.py`（backend CLI 命令名锁定）；
- `test_capability_drift.py`（backend 能力漂移，stub 环境下可执行的部分）。

原则：守卫测试的"豁免登记表"模式（fail both on undeclared violations and stale entries）是门禁体系的样板，新检查一律照此写。

## 6. 健康判据汇总

| 维度 | 判据 |
|---|---|
| 退出码 | CLI == 0；daemon SIGTERM 后 == 0；任何门禁进程超时 → FAIL |
| 日志 | stdout/stderr 文件中出现 `Traceback`、未登记的 `ERROR` → FAIL |
| HTTP | `/api/health` 200；CRUD 往返状态码符合预期 |
| 时延 | 启动到健康 30s 预算；echo 任务终态 60s 预算；issue_pr 链路 120s 预算（超时即 FAIL，不重试） |
| 基线锁定 | `/openapi.json` paths 数、CLI 子命令数、迁移表集合——漂移需显式更新基线文件，**且基线更新必须独立成 commit**（不得与功能变更混入同一 commit），使评审聚焦于"锁定面变化"本身；runner 校验到基线漂移而 diff 中无独立基线 commit 时，报告标注 `baseline-drift-uncommitted` |

## 7. 执行与 CI 集成

### 7.1 入口

```
# 本地 / CI 一致
python tests/gate/gate_runner.py            # 产出 gate-report.json + gate-report.md，exit code 聚合
# 等价于 pytest tests/gate -m gate …（suite 轮）+ GUARD_NODEIDS（G5 轮）+ 报告组装
```

`gate_runner.py` 职责：收集结果 → 组装 JSON（每项 `id/verdict/duration/log_excerpt/hint`）→ 非 PASS 时将报告路径写入 stdout 末行（供自迭代 agent 抓取）。

### 7.2 流水线位置

| 场景 | 套件 | 预算 |
|---|---|---|
| agent PR（自迭代主路径） | G0–G5 全量（含 G4b issue_pr 链路） | ≤15 min |
| 人类 PR | 同上 | ≤15 min |
| nightly | 门禁 + 全量回归 + agentintegration 烟囱（真实 CLI，opt-in） | 不限 |
| 发布前 | 门禁 + 手动 E2E 清单（`tests/manual_e2e_*`） | 不限 |

Postgres 服务由 CI job 级 service container 提供；本地开发者无 Postgres 时，G2/G3/G4 依据豁免登记表（§9）降级为"显式 SKIP(registered)"并在报告中标注——这与 fail-closed 不矛盾：**跳过必须有登记，登记过期即 FAIL**。

### 7.3 自迭代闭环

自 fix 循环中 agent 的行为契约：PR 描述必须附 `gate-report.json` 的 verdict 摘要；FAIL 时 agent 依据 `hint` 字段定位修复后重跑；连续 2 次同检查项 FAIL → 升级为人工，禁止 agent 修改门禁自身（`tests/gate/**` 与基线文件列入 agent 不可写路径，由评审侧强制）。

## 8. 失败分类与 flaky 治理

- **确定性 FAIL**：G0/G1/G5 原则上无环境噪声，FAIL 即真实回归。
- **环境 FAIL**：G2/G3/G4 中因 DB/端口/权限导致的失败需与回归区分：runner 记录失败指纹，豁免登记表自动比对；未登记的环境失败仍判 FAIL（fail-closed），但报告标注 `likely-env` 提示人工复核。
- **flaky 隔离**：任何检查项 14 天内出现 ≥2 次非确定性失败 → 移入 quarantine 清单（登记表一员），nightly 观察；quarantine 项在 PR 门禁中显示为 `WARN` 不阻塞。清单同样有防腐烂：超过 30 天未转正或删除即 FAIL。

## 9. 豁免登记表

`tests/gate/gate_support.py` 中的唯一事实源（落地位置，原设计为 conftest.py——共享设施放 support 模块便于 `import gate_support as g`）：

```python
@dataclass(frozen=True)
class GateExemption:
    check_id: str            # 如 "G4b.issue_pr_chain"
    reason: str              # 为何允许跳过/放宽
    env_gone_condition: str  # 环境恢复时如何自动转正（如 "PG reachable"）
    expires: str             # 绝对日期，如 "2026-12-31"；过期即 FAIL
    platform: str | None = None  # 平台限定，如 "win32"；None = 全平台

GATE_EXEMPTIONS: tuple[GateExemption, ...] = (
    GateExemption(
        check_id="G3.graceful_sigterm_exit_code",
        reason="Windows 原生无 SIGTERM；放宽为 psutil 递归终止 + 无存活子进程 + "
               "日志无未处理异常（§5.4 平台差异）",
        env_gone_condition="不适用——平台能力差异，非环境缺失",
        expires="2027-09-09",   # 年度复核：若引入 POSIX 子系统/WSL 直跑方案则撤销
        platform="win32",
    ),
    GateExemption(
        check_id="G4b.issue_pr_chain",
        reason="P2 待实施：issue→PR 业务链路需 fake git remote + 确定性 stub "
               "回包 fixtures（§5.5.1）",
        env_gone_condition="P2 落地后撤销本条目",
        expires="2026-10-31",
    ),
)
```

> 历史条目 `G2.migrations`（迁移 0009/0010 在分区表 events 上
> CONCURRENTLY 必败）已于 2026-09-10 修复后撤销——豁免条目的生命周期
> 即如此运转：登记 → 修复 → 撤销。

规则与 `test_architecture.py` 完全同构：未登记的失败 FAIL，登记了但环境实际可用、或已过期、或在**非限定平台**上被引用 → 同样 FAIL。新检查上线时若现有代码无法满足，须在此登记并给出消除期限，而不是调低断言。

## 10. 与现有测试体系的关系

| 现有资产 | 关系 |
|---|---|
| 进程内单元/契约测试（tests/ 主体） | 不变，继续承担逻辑正确性；门禁不重复其断言 |
| `tests/api/conftest.py`（PG fixtures、AUTH=1） | G4 复用其建库约定与鉴权约定；但 G2 指出其 create_schema 盲区 |
| `install_cli_shims` / CLI PATH 守卫 | 门禁直接复用 stub，且全量套件继续携带守卫（防误触真实 CLI） |
| `agentintegration` marker | 保持 opt-in，nightly 才跑；门禁永不依赖真实 agent CLI |
| `test_architecture.py` 豁免登记表 | 模式来源；G5 收编之，新检查照此写 |
| `scripts/agent-cli-command-names.txt` 锁定 | G1 的子命令锁定照此模式新增 `expected_cli_subcommands.txt` |

## 11. 实施计划

- **P0（先行，无外部依赖）**：gate marker 与目录骨架、`gate_runner.py`、G0、G1（含子命令锁定文件）、G5 收编。上线即拦截 import 断裂与 CLI 破坏。
- **P1**：G3 启动冒烟（daemon + serve 双变体、stop 变体）、日志扫描与 psutil 清理、豁免登记表机制。
- **P2**：G2 迁移对拍（含 downgrade 与旧库升级路径）、G4 echo 端到端 + CRUD 往返、G4b issue_pr 业务链路（fake git remote + stub 回包，§5.5.1）、基线独立 commit 校验（§6 基线锁定）、`gate-report.json` 接入自迭代闭环。
- **P3**：flaky quarantine 机制、Windows 原生平台豁免与放宽断言（§5.4 平台差异）、nightly 与 agentintegration 打通。

每阶段以"门禁捕获过一次真实回归"作为验收信号（可先在历史 bug 上验证：#34 slug 碰撞、#20 route 命名特判——G3 stop 变体与 G1 锁定本应拦截）。

## 12. 已决事项（原开放问题，2026-09-09 定案）

1. **G4 是否覆盖 issue_pr 业务链路？——覆盖。** G4b（§5.5.1）以 fake git remote + 确定性 stub 回包把 issue→clarifier→dispatch→commit→终态整条链路纳入 PR 门禁必选集，不做 nightly 降级。配套成本（fixtures 约半天实现）已计入 P2，门禁总预算相应放宽至 ≤15 分钟。
2. **门禁基线文件更新是否独立 commit？——是。** 基线（openapi paths、CLI 子命令清单、迁移表集合）的任何变更必须独立成 commit、不与功能变更混合；runner 检测到基线漂移而无独立基线 commit 时报告 `baseline-drift-uncommitted`（§6 基线锁定行）。
3. **Windows 进程清理差异是否豁免登记？——登记。** `G3.graceful_sigterm_exit_code` 以 `platform="win32"` 条件豁免入表（§9），断言放宽为 psutil 递归终止 + 无存活子进程 + 日志无未处理异常；WSL2/Linux 路径不受影响，豁免被非限定平台引用即 FAIL。

## 13. 落地实测（2026-09-09，WSL2 / 8 核 / PG 本机）

`python tests/gate/gate_runner.py` 全量通过轮的实测分层耗时：

| Layer | 内容 | PASS/FAIL/SKIP | 耗时 |
|---|---|---|---|
| G0 | compileall + 全模块 import + ruff(E9) | 3/0/0 | 11.1s |
| G1 | --version + 16 子命令 --help + 2 只读真实执行 | 20/0/0 | 100.4s |
| G2 | 迁移对拍（G2.migrations 豁免 → SKIP×3） | 0/0/3 | 20.6s |
| G3 | daemon start/serve/stop 三变体 | 3/0/0 | 47.5s |
| G4 | CRUD 往返 + echo 协议环（G4b 登记豁免 SKIP） | 2/0/1 | 15.3s |
| G5 | 守卫收编（4 文件） | 39/0/5 | 6.2s |
| **合计** | | | **≈3.7 min** |

远低于 ≤15 min 预算；G1 占近半耗时（每次 `--help` 真实子进程 ~5s），后续若需压缩可合并为单进程批量执行（牺牲部分隔离性）。

落地过程中捕获/确认的真实发现（门禁设计价值的直接证据）：

1. **迁移 0009/0010 真实缺陷（已修复 2026-09-10）**：分区表 `events` 上 `CREATE INDEX CONCURRENTLY` 被 PG 拒绝，全新库 `alembic upgrade head` 必败——现有测试从不跑迁移，此缺陷长期潜伏。修复：两条迁移改为普通 CREATE INDEX（迁移时父表零分区，瞬时完成；后续挂载的月分区自动继承索引），G2.migrations 豁免随之撤销，G2 三项转 PASS。
2. **ruff 存量债 963 条**（§5.1）：仓库从未强制 lint，G0 收窄为 E9-only 并留季度收紧路线。
3. **G3 stop 变体的僵尸进程伪影（产品侧已修复 2026-09-10）**：daemon 是 pytest 子进程，退出后成僵尸；`server stop`（独立进程）的 `_is_pid_alive` 用 signal 0 探测，僵尸仍报存活 → 误判超时 rc=1。真实用户场景 daemon 被 init 收割无此问题，测试侧以后台收割线程模拟 reap（`test_g3_startup.py`）。daemon 实测 SIGTERM 后 0.4s 优雅退出。产品修复：`_is_pid_alive` 在 Linux 上读 `/proc/<pid>/stat` 把 Z 状态判为已死亡（非 Linux 回退 signal 0 语义）。
4. **uvicorn ≥0.52 信号语义**（§5.4）：serve 变体退出码判据据实放宽为 {0, -15}。
