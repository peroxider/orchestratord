# Agent 可调用 Skills 系统方案 — 设计文档

> **状态：** 临时设计稿，待评审。
> **目标：** 引入"skill"一等公民对象：一个 skill 是 agent 运行期可调用的领域知识包，包含 SKILL.md（agent 可见文档）、`references/source-map.md`（每条声明钉到源码行号的可验证引用），以及可选 `tools.py`（agent 调用的 Python 工具）。所有内置 skill 在 pytest 与 daemon 启动期做 source-map 校验，文档腐烂即被 CI 拦截。
> **参考：** multica `server/internal/service/builtin_skills/`（9 个 agent-callable skill，结构 SKILL.md + references/*-source-map.md）、`CLI_AND_DAEMON.md:817-890`（autopilot 描述）、multica `builtin_skills/multica-mentioning/SKILL.md:4-5`（`user-invocable: false, allowed-tools: Bash(multica *)`）。
> **不解决：** Skill 编辑器（UI）；Skill 远程分发（市场）；Skill 之间的依赖图。

---

## 0. 背景与现状

### 0.1 现状缺口

`orchestratord` 当前没有"skill"抽象。最接近的是：

- `agent_task.py` + `agent_task_runner.py` — 内部任务抽象（开发期用，非 agent 调用）
- `modes/{single,pipeline,coordinator,debate,swarm}.py` — 编排模式（路由层用）
- `extensions.sop_converter.bundle_skills.register_bundle_skills`（`orchestration_subsystem.py:82-89`）— clawcodex 内部用，与 orchestratord 核心解耦但未抽出公共契约
- `agent_runner.py:1355` 的 `system_prompt_append` 拼接位 — 是 skill 注入的天然锚点但目前只拼 workflow 上下文

问题：
1. **没有 skill 加载机制**：orchestrator 想给 agent 注入"如何选 mode / 如何读 capability / 如何从失败恢复"这类领域知识，要么硬编码到 prompt 模板里，要么写到 SKILL.md 但没有加载管线
2. **没有 skill 验证**：文档与代码脱节是普遍痛点 — 设计文档里写的"agent 应当 X"几个月后没人记得是哪个版本
3. **没有 skill CLI**：operator 无法"列出当前可用 skill / 看 skill 详情 / 验证 skill 是否还正确"

### 0.2 multica 对应设计

| 概念 | multica 位置 | 含义 |
|---|---|---|
| Skill 包 | `server/internal/service/builtin_skills/<name>/` | 一个 skill 一个目录 |
| SKILL.md | `multica-*/SKILL.md` | agent 读 frontmatter + Markdown |
| references/source-map.md | `references/<name>-source-map.md` | 每条声明钉源文件 + 行号 |
| allowed-tools | SKILL.md `allowed-tools: Bash(multica *)` | skill 可调用的工具白名单 |
| user-invocable | SKILL.md `user-invocable: false` | 仅 agent 调用 |
| 加载时机 | `builtin_agents.go:17-58` | daemon 启动时 `go:embed` 全部 |

### 0.3 方案总览

| # | 子方案 | 范围 | 解决 |
|---|---|---|---|
| A | Skill 包目录结构与 frontmatter | 新增 `src/orchestratord/skills/` 协议 | 可发现 |
| B | Skill loader + source-map 验证 | 新增 `src/orchestratord/skills/loader.py` | 启动期校验 |
| C | Skill 注入到 system_prompt | `agent_runner.py:1355` 改造 | agent 可调用 |
| D | Skill CLI | 新增 `orchestratord skills {list,show,verify}` | operator 可观测 |
| E | 3 个 starter skills | `builtin/{mode_selector,capability_explainer,failure_recovery}/` | 立即有内容 |

---

## 1. 方案 A — Skill 包目录结构

### 1.1 目标

定义"一个 skill 是什么"的物理形态。任何 skill 是一个目录，含：

```
skills/builtin/<skill_name>/
├── SKILL.md              # 必填 — frontmatter + Markdown 正文（agent 读）
├── references/
│   └── source-map.md     # 必填 — 每条声明钉源码 + 行号
└── tools.py              # 可选 — skill 自带 Python 工具（agent 可 import）
```

### 1.2 SKILL.md 格式

```markdown
---
name: mode-selector
display_name: Mode Selector
description: 帮助 agent 在 single / pipeline / coordinator / debate / swarm 间选择
user_invocable: false
allowed_tools: []            # 列出允许的 tools.py 函数名；空表示无工具
version: 1
---

# Mode Selector

## 何时使用

当 orchestrator 需要决定 issue 走哪种 mode 时调用本 skill。

## 选择规则

参考 orchestratord 源码：
- 关键词含 "review / 评审" → coordinator
- 关键词含 "design / debate" → debate
- ...

## 输出

返回选定的 mode 名字符串。
```

frontmatter 字段：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `name` | 是 | str | kebab-case，全局唯一 |
| `display_name` | 是 | str | 用户可见名 |
| `description` | 是 | str | 一句话功能 |
| `user_invocable` | 否 | bool | 默认 false（仅 agent 调） |
| `allowed_tools` | 否 | list[str] | tools.py 中允许 agent 调用的函数名；空=无工具 |
| `version` | 否 | int | 默认 1 |

### 1.3 source-map.md 格式

```markdown
# Source Map — mode-selector

本文件把 SKILL.md 的每条声明钉到 orchestratord 仓库的源文件与行号。
loader 在启动期校验：文件存在 + 行号内容 hash 匹配。
任何源文件修改触发本 skill 文档 review。

## 引用表

| 声明 | 源文件 | 行号 | 期望 hash（前 8 字符 SHA256）|
|---|---|---|---|
| "关键词含 review → coordinator" | `src/orchestratord/mode_router.py` | 126-140 | a1b2c3d4 |
| "HeuristicRouter 桶定义" | `src/orchestratord/mode_router.py` | 60-90 | e5f6g7h8 |
| "KNOWN_MODES 列表" | `src/orchestratord/mode_selector.py` | 44 | i9j0k1l2 |
```

校验算法：loader 读 `src/orchestratord/mode_router.py:126-140` 内容 → SHA256 → 与期望 hash 比较。任一不匹配则 skill 标 `stale: true`，daemon 启动期警告 + CLI `verify` 命令失败。

### 1.4 验收

- 一个完整 skill 目录能在 `src/orchestratord/skills/builtin/mode_selector/` 列出 3 个文件
- frontmatter 用 `python -c "import yaml; yaml.safe_load(...)"` 可解析

---

## 2. 方案 B — Skill loader + source-map 验证

### 2.1 目标

daemon 启动期（或 pytest setup）加载所有内置 skill，校验 source-map，发现腐烂立即报告。

### 2.2 设计

```python
# src/orchestratord/skills/loader.py (新)
"""Skill discovery + source-map validation.

加载所有 src/orchestratord/skills/builtin/*/SKILL.md，解析 frontmatter，
对每个 references/source-map.md 做行号 + 内容 hash 校验。
"""

from __future__ import annotations
import hashlib
import importlib
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillSourceMapRef:
    claim: str
    file_path: str        # 相对仓库根的路径，如 "src/orchestratord/mode_router.py"
    start_line: int
    end_line: int
    expected_sha256_prefix: str


@dataclass(frozen=True)
class Skill:
    name: str
    display_name: str
    description: str
    user_invocable: bool
    allowed_tools: tuple[str, ...]
    version: int
    skill_md_path: Path
    source_map: tuple[SkillSourceMapRef, ...]
    tools_module: object | None
    is_stale: bool = False       # source-map 校验失败时置 True
    stale_reasons: tuple[str, ...] = ()


_SKILL_ROOT = Path(__file__).resolve().parent / "builtin"


def load_all_skills(*, repo_root: Path | None = None, validate: bool = True) -> list[Skill]:
    """扫描 _SKILL_ROOT 下所有 skill 目录；返回 Skill 列表。

    validate=False：跳过 source-map 校验（用于 `--no-verify` CLI 模式）。
    """
    skills: list[Skill] = []
    for skill_dir in sorted(_SKILL_ROOT.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        skill = _load_one_skill(skill_dir, skill_md, repo_root, validate)
        skills.append(skill)
    return skills


def _load_one_skill(skill_dir, skill_md, repo_root, validate) -> Skill:
    text = skill_md.read_text()
    fm, body = _split_frontmatter(text)
    meta = yaml.safe_load(fm) or {}
    source_map = _load_source_map(skill_dir / "references" / "source-map.md")
    tools_module = _maybe_load_tools(skill_dir / "tools.py")

    stale = False
    reasons: list[str] = []
    if validate and source_map:
        for ref in source_map:
            ok, reason = _validate_ref(ref, repo_root)
            if not ok:
                stale = True
                reasons.append(reason)

    return Skill(
        name=meta["name"],
        display_name=meta["display_name"],
        description=meta["description"],
        user_invocable=meta.get("user_invocable", False),
        allowed_tools=tuple(meta.get("allowed_tools", [])),
        version=meta.get("version", 1),
        skill_md_path=skill_md,
        source_map=tuple(source_map),
        tools_module=tools_module,
        is_stale=stale,
        stale_reasons=tuple(reasons),
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    """分离 YAML frontmatter 与 Markdown 正文。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError("SKILL.md missing frontmatter delimiters '---'")
    return m.group(1), m.group(2)


def _load_source_map(path: Path) -> list[SkillSourceMapRef]:
    """解析 source-map.md 表格 → SkillSourceMapRef 列表。"""
    if not path.exists():
        return []
    refs: list[SkillSourceMapRef] = []
    for line in path.read_text().splitlines():
        m = re.match(
            r"\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(\d+)-(\d+)\s*\|\s*([0-9a-f]{8})\s*\|",
            line,
        )
        if m:
            refs.append(
                SkillSourceMapRef(
                    claim=m.group(1),
                    file_path=m.group(2),
                    start_line=int(m.group(3)),
                    end_line=int(m.group(4)),
                    expected_sha256_prefix=m.group(5),
                )
            )
    return refs


def _validate_ref(ref: SkillSourceMapRef, repo_root: Path | None) -> tuple[bool, str]:
    """读 ref.file_path:ref.start_line..end_line；与期望 hash 比对。"""
    root = repo_root or _find_repo_root()
    target = root / ref.file_path
    if not target.exists():
        return False, f"{ref.claim}: file not found: {ref.file_path}"
    lines = target.read_text().splitlines()
    chunk = "\n".join(lines[ref.start_line - 1 : ref.end_line])
    actual = hashlib.sha256(chunk.encode()).hexdigest()[:8]
    if actual != ref.expected_sha256_prefix:
        return False, (
            f"{ref.claim}: hash mismatch at {ref.file_path}:"
            f"{ref.start_line}-{ref.end_line} expected={ref.expected_sha256_prefix}"
            f" actual={actual}"
        )
    return True, ""


def _maybe_load_tools(path: Path):
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("skill_tools", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_repo_root() -> Path:
    """从当前文件向上找最近的 pyproject.toml。"""
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "pyproject.toml").exists():
            return cur
        cur = cur.parent
    return Path.cwd()
```

### 2.3 改动清单

- `src/orchestratord/skills/__init__.py` — 新建（暴露 `load_all_skills`）
- `src/orchestratord/skills/loader.py` — 新建
- `src/orchestratord/skills/builtin/` — 新建目录

### 2.4 验收

- `python -c "from orchestratord.skills import load_all_skills; print(load_all_skills())"` 输出 3 个 Skill
- 任一 source-map 行号错（移到下一行）→ `is_stale=True` 且 `stale_reasons` 含具体信息
- 任一期望 hash 改 1 字符 → 同上

---

## 3. 方案 C — Skill 注入到 system_prompt

### 3.1 目标

让 agent 在每次 session 创建时收到所有 skill 的 SKILL.md 摘要（不是全部正文 — agent 会按需调用 `load_skill(name)` 工具取正文）。

### 3.2 设计

`agent_runner.py:1355` 当前拼接 `system_prompt_append = ""`，改为：

```python
# src/orchestratord/agent_runner.py:1355 附近
from orchestratord.skills import load_all_skills

skills = load_all_skills(validate=False)  # 校验在 daemon 启动期已做
skill_index = "\n".join(
    f"- {s.name}: {s.description}"
    for s in skills
)
system_prompt_append = (
    f"{base_append}\n\n"
    f"## 可用 Skills（agent 可调用）\n{skill_index}\n\n"
    f"调用方式：使用 `load_skill(name='<name>')` 工具获取完整 SKILL.md。"
)
```

新增内置工具 `load_skill(name: str) -> str`：

```python
# src/orchestratord/skills/tools.py (新)
"""Skill 调用工具 — agent 通过它读完整 SKILL.md。"""

from orchestratord.skills.loader import load_all_skills

_SKILLS: list = load_all_skills(validate=False)
_BY_NAME: dict[str, object] = {s.name: s for s in _SKILLS}


def load_skill(name: str) -> str:
    """Read the full SKILL.md body for `name`.

    Args:
        name: skill name (kebab-case)

    Returns:
        SKILL.md 正文（不含 frontmatter）

    Raises:
        SkillNotFoundError: skill 未注册
    """
    skill = _BY_NAME.get(name)
    if skill is None:
        raise SkillNotFoundError(name)
    text = skill.skill_md_path.read_text()
    _, body = _split_frontmatter(text)
    return body
```

在 5 个 backend 包的 SPI 工具注册点注入 `load_skill`：

- clawcodex: 通过 `tools.py` 暴露
- codex AppServer: 注入 `tools` 字段
- dsh: 注入 `harness.tools`
- hermes: CLI 参数 `--tools` 注入 JSON 描述
- opencode: SSE `tools` 字段

### 3.3 改动清单

- `src/orchestratord/agent_runner.py:1355` — 拼接 skill index
- `src/orchestratord/skills/tools.py` — 新建（`load_skill` 工具）
- 5 个 backend 包 SPI 工具注册 — 各自增 `load_skill` 暴露

### 3.4 验收

- agent session 启动时 system prompt 含 "## 可用 Skills" 段 + 3 行 index
- `load_skill("mode-selector")` 返回该 skill 的正文
- `load_skill("nonexistent")` 抛 `SkillNotFoundError`

---

## 4. 方案 D — Skill CLI

### 4.1 目标

operator 能在终端直接看 skill 状态：

- `orchestratord skills list` — 列出所有 skill 名称 + 描述 + 是否 stale
- `orchestratord skills show <name>` — 完整 SKILL.md + source-map
- `orchestratord skills verify` — 重跑所有 source-map 校验，exit code 反映是否全 fresh

### 4.2 设计

新增 Typer 子命令（项目主 CLI 已是 Typer，见 `pyproject.toml` `[project.scripts]`）：

```python
# src/orchestratord/cli/skills.py (新)
import typer
from orchestratord.skills.loader import load_all_skills

app = typer.Typer(help="Skill 管理。")


@app.command("list")
def list_cmd():
    """列出所有 skill。"""
    skills = load_all_skills()
    typer.echo(f"{'NAME':<24} {'STALE':<6} DESCRIPTION")
    for s in skills:
        stale_marker = "YES" if s.is_stale else "no"
        typer.echo(f"{s.name:<24} {stale_marker:<6} {s.description}")


@app.command("show")
def show_cmd(name: str):
    """显示 skill 完整内容（SKILL.md + source-map）。"""
    skills = {s.name: s for s in load_all_skills()}
    skill = skills.get(name)
    if not skill:
        typer.echo(f"skill {name!r} not found", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"--- SKILL.md ({skill.skill_md_path}) ---")
    typer.echo(skill.skill_md_path.read_text())
    if skill.source_map:
        typer.echo("\n--- source-map.md ---")
        typer.echo((skill.skill_md_path.parent / "references" / "source-map.md").read_text())


@app.command("verify")
def verify_cmd():
    """校验所有 skill source-map；exit 0 = 全 fresh，非 0 = 有 stale。"""
    skills = load_all_skills()
    stale = [s for s in skills if s.is_stale]
    if stale:
        typer.echo(f"{len(stale)} stale skills:", err=True)
        for s in stale:
            typer.echo(f"  {s.name}:", err=True)
            for r in s.stale_reasons:
                typer.echo(f"    - {r}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"all {len(skills)} skills verified fresh")
```

注册到主 CLI：`src/orchestratord/cli/main.py` 加 `app.add_typer(skills.app, name="skills")`。

### 4.3 验收

- `orchestratord skills list` 输出 3 行
- `orchestratord skills show mode-selector` 输出完整 SKILL.md + source-map
- 改 source-map 期望 hash → `orchestratord skills verify` 退出码 1

---

## 5. 方案 E — 3 个 starter skills

### 5.1 目标

启动即有 3 个真有用 skill，每个含 SKILL.md + source-map.md。

### 5.2 三个 skill

| Skill 名 | 用途 | 关键 source-map 引用 |
|---|---|---|
| `mode-selector` | 帮 orchestrator 在 single / pipeline / coordinator / debate / swarm 间选择 | `mode_selector.py:44` (KNOWN_MODES), `mode_router.py:126-180` (HeuristicRouter) |
| `capability-explainer` | agent 看到 capability 位时给出"这意味着什么" | `spi/capabilities.py:13` (BackendCapabilities 字段), ADR-002 (goal_mode 引入) |
| `failure-recovery` | agent 收到 ERROR / SESSION_COMPLETE(reason=error) 时决定下一步（retry / fallback / abort） | `failure_messages.py:41` (现有文案), `orchestrator.py:4132-4324` (`_schedule_retry` / `_process_retry_queue`) |

### 5.3 `mode-selector/SKILL.md` 示例

```markdown
---
name: mode-selector
display_name: Mode Selector
description: 帮 orchestrator 在 single / pipeline / coordinator / debate / swarm 间选择合适的多 agent 编排模式
user_invocable: false
allowed_tools: []
version: 1
---

# Mode Selector

## 何时使用

当 orchestrator 收到一个 issue 但没有显式 `mode:` label，需要推断用哪个 mode。

## 选择规则

1. **关键词 review / 评审 / feedback**：coordinator
2. **关键词 design / compare / vs**：debate
3. **关键词 parallel / concurrently / 各自**：swarm
4. **关键词 sequential / 先后**：pipeline
5. **默认**：single

详见 `mode_router.py:126-180` 的 `HeuristicRouter.bucketize()`。

## 输出

返回 mode 名字符串。
```

### 5.4 `mode-selector/references/source-map.md`

```markdown
# Source Map — mode-selector

| 声明 | 源文件 | 行号 | 期望 hash |
|---|---|---|---|
| "KNOWN_MODES 列表" | src/orchestratord/mode_selector.py | 44-45 | <运行后填> |
| "HeuristicRouter.bucketize 关键词桶" | src/orchestratord/mode_router.py | 126-180 | <运行后填> |
```

source-map 中 hash 在首次落盘时由 `tools/scripts/regen_source_map.py`（辅助脚本，本设计不强制）填入 — loader 不强制初始 hash 必须填，可暂时填 8 个 0，待下一次 skill 编辑时回填真值。

### 5.5 改动清单

- `src/orchestratord/skills/builtin/mode-selector/SKILL.md` — 新建
- `src/orchestratord/skills/builtin/mode-selector/references/source-map.md` — 新建
- `src/orchestratord/skills/builtin/capability-explainer/SKILL.md` — 新建
- `src/orchestratord/skills/builtin/capability-explainer/references/source-map.md` — 新建
- `src/orchestratord/skills/builtin/failure-recovery/SKILL.md` — 新建
- `src/orchestratord/skills/builtin/failure-recovery/references/source-map.md` — 新建

---

## 6. 完整改动清单

| 文件 | 改动 | 行数估计 |
|---|---|---|
| `src/orchestratord/skills/__init__.py` | 新建 | ~10 |
| `src/orchestratord/skills/loader.py` | 新建 | ~150 |
| `src/orchestratord/skills/tools.py` | 新建（`load_skill` 工具） | ~30 |
| `src/orchestratord/skills/builtin/mode-selector/SKILL.md` | 新建 | ~30 |
| `src/orchestratord/skills/builtin/mode-selector/references/source-map.md` | 新建 | ~10 |
| `src/orchestratord/skills/builtin/capability-explainer/SKILL.md` | 新建 | ~30 |
| `src/orchestratord/skills/builtin/capability-explainer/references/source-map.md` | 新建 | ~10 |
| `src/orchestratord/skills/builtin/failure-recovery/SKILL.md` | 新建 | ~30 |
| `src/orchestratord/skills/builtin/failure-recovery/references/source-map.md` | 新建 | ~10 |
| `src/orchestratord/agent_runner.py:1355` | 拼接 skill index | +15 |
| 5 个 backend 包 SPI 工具注册 | 暴露 `load_skill` | +30/包 |
| `src/orchestratord/cli/skills.py` | 新建 Typer 子命令 | ~50 |
| `src/orchestratord/cli/main.py` | 注册 `skills` 子命令 | +3 |
| **新测试** `tests/test_skills_loader.py` | loader + source-map 校验 | ~120 |
| **新测试** `tests/test_skills_cli.py` | CLI 子命令 | ~60 |
| **新测试** `tests/test_skill_injection.py` | agent_runner 注入 | ~50 |
| **新辅助脚本** `scripts/regen_source_map.py` | 重新计算 hash | ~40 |

总计新增 12 文件 + 改 8 文件；代码约 580 行。

---

## 7. 关键代码预览

### 7.1 loader 核心

```python
def load_all_skills(*, repo_root=None, validate=True) -> list[Skill]:
    skills = []
    for skill_dir in sorted(_SKILL_ROOT.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        skills.append(_load_one_skill(skill_dir, skill_md, repo_root, validate))
    return skills
```

### 7.2 source-map 校验

```python
def _validate_ref(ref, repo_root) -> tuple[bool, str]:
    root = repo_root or _find_repo_root()
    target = root / ref.file_path
    lines = target.read_text().splitlines()
    chunk = "\n".join(lines[ref.start_line - 1 : ref.end_line])
    actual = hashlib.sha256(chunk.encode()).hexdigest()[:8]
    if actual != ref.expected_sha256_prefix:
        return False, f"{ref.claim}: hash mismatch at {ref.file_path}:{ref.start_line}-{ref.end_line}"
    return True, ""
```

### 7.3 agent_runner 注入

```python
skills = load_all_skills(validate=False)
skill_index = "\n".join(f"- {s.name}: {s.description}" for s in skills)
system_prompt_append = (
    f"{base_append}\n\n"
    f"## 可用 Skills\n{skill_index}\n\n"
    f"调用方式：load_skill(name='<name>')"
)
```

---

## 8. 验收标准（Verification）

- [ ] **3 个 starter skill 目录存在**：`ls src/orchestratord/skills/builtin/` 看到 3 个
- [ ] **`load_all_skills()` 返回 3 个 Skill**：每个有非空 description + source-map
- [ ] **source-map 校验正确**：临时改 1 个 source-map 期望 hash → `is_stale=True`
- [ ] **`orchestratord skills list`** 输出 3 行
- [ ] **`orchestratord skills show mode-selector`** 输出完整 SKILL.md + source-map
- [ ] **`orchestratord skills verify`** 全 fresh 时退出码 0，有 stale 时退出码 1
- [ ] **agent session 启动时 system_prompt 含 "## 可用 Skills" 段**：`test_skill_injection.py` 单测
- [ ] **`load_skill("mode-selector")` 返回正文**；`load_skill("nonexistent")` 抛 `SkillNotFoundError`
- [ ] **CI 守护**：在 `tests/test_capability_drift.py` 加 1 个 case：所有 skill 必须含 `name/display_name/description` frontmatter 字段，否则 fail
- [ ] **现有测试套不破**：94 个 test 文件全部仍 PASS
- [ ] **README 更新**：在"内部架构"段加 1 节介绍 skill 系统 + `orchestratord skills verify` 的用途

---

## 9. 范围外（Out of Scope）

1. **Skill 编辑器 UI**：本设计只提供 CLI；UI 属未来工作
2. **Skill 远程分发 / 市场**：本设计只支持内置 + 自定义；远程分发需另立设计
3. **Skill 间的依赖图**：本设计每个 skill 独立；后续若需要"skill A 依赖 skill B"另议
4. **Skill 版本演进 / 多版本并存**：每个 skill 一个版本；升级即覆盖
5. **Skill 的 LLM 自动生成**：source-map 必须人手维护；不让 LLM 改 frontmatter
6. **`user_invocable: true` 的 skill 本次不实现**：所有 starter skill 均为 `user_invocable: false`（仅 agent 可调）

---

## 10. 后续（Future Work）

- Skill 子集按 backend 过滤：`BackendDescriptor.skills: tuple[str, ...]` 字段控制哪些 skill 注入哪个 backend
- Skill 与 `modes/` 联动：skill 可声明"仅在 mode=X 时注入"
- Skill 调用 metrics：`load_skill` 调用次数 / stale rate dashboard
- 在 `chat_gateway.py` 暴露 skill 列表给 UI
- 与 `DESIGN_two_tier_backend_registry.md` 联动：把 skill 列表移到 `BackendDescriptor.skills`

---

## 11. 参考资料

- multica `server/internal/service/builtin_skills/`（9 个 skill 实现范例）
- multica `CLI_AND_DAEMON.md:817-890`（autopilot 设计语言）
- multica `builtin_skills/multica-mentioning/SKILL.md:4-5`（frontmatter 字段语义）
- orchestratord `agent_runner.py:1355`（system_prompt_append 锚点）
- orchestratord `extensions.sop_converter.bundle_skills`（clawcodex 内部同类物）
- ADR-001 / ADR-002（capability 位与 skill 文档交叉引用）
- `DESIGN_chat_gateway.md` §0.2（已识别 `system_prompt_append` 为可复用锚点）