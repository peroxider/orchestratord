"""Architecture guardrails — DESIGN_ORCHESTRATION_BUSINESS_DECOUPLING.md P0.

Dependency rule B1 (§3.2): mechanism-domain modules must not import
business-domain modules. Business domain = issue_pr 应用及其依赖
(issue_registry, issue_clarifier, repo_tracker, linear, local_tracker,
review_feedback, repro_gate, premise_check, intent, tracker, tracker_kinds,
approval_policy, applications, git)。机制域可自由 import 机制域与共享基础设施
(config, events, sinks, runtime, ...)。

Enforcement is AST-based: no module under test is imported at collection
time, so a broken import cannot hide a violated rule.

Exemptions are registered explicitly with the count of import sites, the
reason, and the phase that eliminates them (§7). The test fails both on
undeclared violations *and* on stale registry entries, so the exemption
list cannot rot silently:

* backend_runner.py — Layer 1 执行器直接读取 session.conflict_files
  （prompt 装饰透传，FIELD_READ_EXEMPTIONS，P4 消除）。import 域豁免
  （approval_policy / git / VerificationFailed / TYPE_CHECKING Issue）
  已随 P4 机制归位全部消除，EXEMPTIONS 现为空。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "orchestratord"

#: 机制域模块（DESIGN §1.2 表 + §7 P0 范围 + B1 规则）。键为相对 SRC_ROOT
#: 的路径，目录会递归扫描。
MECHANISM_PATHS: tuple[str, ...] = (
    "kernel",
    "spi",
    "agent",
    "modes",
    "workflow_engine",
    "events",
    "sinks",
    "mode_selector.py",
    "mode_router.py",
    "backend_runner.py",
    "workflow_orchestrator.py",
)

#: 业务域顶层模块名（DESIGN 附录 B「业务 (issue_pr)」行）。
BUSINESS_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "issue_registry",
        "issue_clarifier",
        "repo_tracker",
        "linear",
        "local_tracker",
        "review_feedback",
        "repro_gate",
        "premise_check",
        "intent",
        "tracker",
        "tracker_kinds",
        "approval_policy",
        "applications",
        "git",
    }
)


@dataclass(frozen=True)
class Exemption:
    """一条已登记的依赖豁免（file, 顶层业务模块, 引用形态）。"""

    file: str
    business_top: str
    type_checking_only: bool
    count: int
    reason: str
    eliminated_in: str


EXEMPTIONS: tuple[Exemption, ...] = ()


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    business_top: str
    type_checking_only: bool


def _is_type_checking_guard(node: ast.AST) -> bool:
    """True 若 node 是 ``if TYPE_CHECKING:`` 的测试表达式。"""
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    return False


def _resolve_from_top(node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return _resolve_absolute_top(node.module or "")
    # level>=1：相对当前包上溯 (level-1) 层。
    module = node.module or ""
    first = module.split(".")[0] if module else ""
    if first:
        return first
    # ``from . import X`` → 被导入名即顶层名。
    for alias in node.names:
        return alias.name.split(".")[0]
    return None


def _resolve_absolute_top(module_name: str) -> str | None:
    parts = module_name.split(".")
    if parts[0] == "orchestratord":
        return parts[1] if len(parts) > 1 else None
    # pythonpath=src 布局下机制域内部一律用相对导入或 orchestratord.*
    # 绝对导入；裸顶层名同样视为 orchestratord 包内模块。
    return parts[0]


def _scan_violations(rel_file: str, tree: ast.Module) -> list[Violation]:
    found: list[Violation] = []
    stack: list[tuple[ast.AST, bool]] = [(tree, False)]
    while stack:
        node, guarded = stack.pop()
        for child in ast.iter_child_nodes(node):
            child_guarded = guarded or (
                isinstance(child, ast.If) and _is_type_checking_guard(child.test)
            )
            if isinstance(child, ast.ImportFrom):
                top = _resolve_from_top(child)
                if top in BUSINESS_TOP_LEVEL:
                    found.append(Violation(rel_file, child.lineno, top, child_guarded))
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    top = _resolve_absolute_top(alias.name)
                    if top in BUSINESS_TOP_LEVEL:
                        found.append(Violation(rel_file, child.lineno, top, child_guarded))
            stack.append((child, child_guarded))
    return found


@dataclass(frozen=True)
class FieldReadExemption:
    """一条机制域直接读取 RunSession 业务属性的已登记豁免（P3 规则）。"""

    file: str
    attr: str
    count: int
    reason: str
    eliminated_in: str


#: P3 起 clarification/conflict_files/attempt 类业务字段的存储迁移到
#: ``RunSession.business``（session_state.py 以同名 property 兼容）。
#: 机制域对这些属性名的直接读取必须登记豁免，随 Phase 消除（§7）。
BUSINESS_ATTRS: frozenset[str] = frozenset(
    {
        "clarification_question",
        "clarification_answer",
        "clarification_source",
        "conflict_files",
        "summary_comment_id",
        "issue_attempt",
        "followup_attempt",
    }
)

FIELD_READ_EXEMPTIONS: tuple[FieldReadExemption, ...] = (
    FieldReadExemption(
        file="backend_runner.py",
        attr="conflict_files",
        count=1,
        reason="rebase 冲突文件作为 prompt 装饰参数透传 PromptBuilder.render_parts；"
        "P4 prompt 装配随 prepare_run 迁应用侧后消除",
        eliminated_in="P4",
    ),
)


def _iter_mechanism_trees():
    """Yield (rel_file, AST) for every mechanism-domain module."""
    for rel in MECHANISM_PATHS:
        path = SRC_ROOT / rel
        if path.is_dir():
            files = ((p.relative_to(SRC_ROOT).as_posix(), p) for p in sorted(path.rglob("*.py")))
        elif path.is_file():
            files = ((rel, path),)
        else:
            continue
        for rel_file, py in files:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            yield rel_file, tree


def _collect_all_violations() -> list[Violation]:
    violations: list[Violation] = []
    for rel_file, tree in _iter_mechanism_trees():
        violations.extend(_scan_violations(rel_file, tree))
    return violations


def _signature_key(v: Violation) -> tuple[str, str, bool]:
    return (v.file, v.business_top, v.type_checking_only)


def test_mechanism_domain_has_no_business_imports() -> None:
    found = _collect_all_violations()
    registry = {(e.file, e.business_top, e.type_checking_only): e.count for e in EXEMPTIONS}
    actual: dict[tuple[str, str, bool], list[Violation]] = {}
    for v in found:
        actual.setdefault(_signature_key(v), []).append(v)

    undeclared = {k: v for k, v in actual.items() if k not in registry}
    stale = [k for k in registry if k not in actual]
    count_mismatch = {
        k: (registry[k], len(v))
        for k, v in actual.items()
        if k in registry and registry[k] != len(v)
    }

    problems: list[str] = []
    if undeclared:
        details = "\n".join(
            f"  {v.file}:{v.line} imports business module '{v.business_top}'"
            f"{' (TYPE_CHECKING)' if v.type_checking_only else ''}"
            for vs in undeclared.values()
            for v in vs
        )
        problems.append(
            "机制域出现未登记的业务域 import（B1 规则，DESIGN §3.2）。"
            "请消除依赖；确属过渡期豁免则在 EXEMPTIONS 登记：\n" + details
        )
    if stale:
        problems.append(
            "EXEMPTIONS 中存在已失效条目（违规已消除，请从登记表删除）：\n"
            + "\n".join(f"  {k}" for k in stale)
        )
    if count_mismatch:
        problems.append(
            "EXEMPTIONS 计数与实际 import 点不一致 (file, top, type_checking) ->"
            " (登记数, 实际数)：\n"
            + "\n".join(
                f"  {k}: {expected} != {actual_n}"
                for k, (expected, actual_n) in count_mismatch.items()
            )
        )
    assert not problems, "\n\n".join(problems)


def test_mechanism_domain_does_not_read_session_business_fields() -> None:
    """P3（DESIGN §4.3）：RunSession 业务字段存储已迁 ``business`` dict，
    机制域必须零直接读取（同名 property 仅业务/装配侧使用）。豁免走与
    import 豁免相同的登记-计数-消除流程，防止静默漂移。"""
    found: list[tuple[str, int, str]] = []
    for rel_file, tree in _iter_mechanism_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in BUSINESS_ATTRS:
                found.append((rel_file, node.lineno, node.attr))

    registry = {(e.file, e.attr): e.count for e in FIELD_READ_EXEMPTIONS}
    actual: dict[tuple[str, str], list[int]] = {}
    for rel_file, lineno, attr in found:
        actual.setdefault((rel_file, attr), []).append(lineno)

    undeclared = {k: v for k, v in actual.items() if k not in registry}
    stale = [k for k in registry if k not in actual]
    count_mismatch = {
        k: (registry[k], len(v))
        for k, v in actual.items()
        if k in registry and registry[k] != len(v)
    }

    problems: list[str] = []
    if undeclared:
        details = "\n".join(
            f"  {rel_file}:{lineno} reads session business attr '{attr}'"
            for (rel_file, attr), linenos in undeclared.items()
            for lineno in linenos
        )
        problems.append(
            "机制域直接读取 RunSession 业务字段（DESIGN §4.3 P3）。"
            "请经 business dict/RunContext 或 accessor 访问；确属过渡期豁免"
            "则在 FIELD_READ_EXEMPTIONS 登记：\n" + details
        )
    if stale:
        problems.append(
            "FIELD_READ_EXEMPTIONS 中存在已失效条目（请删除）：\n"
            + "\n".join(f"  {k}" for k in stale)
        )
    if count_mismatch:
        problems.append(
            "FIELD_READ_EXEMPTIONS 计数与实际读取点不一致 (file, attr) ->"
            " (登记数, 实际数)：\n"
            + "\n".join(
                f"  {k}: {expected} != {actual_n}"
                for k, (expected, actual_n) in count_mismatch.items()
            )
        )
    assert not problems, "\n\n".join(problems)


def test_exemptions_are_phase_tagged() -> None:
    """每条豁免必须声明消除它的 Phase（DESIGN §7「逐 Phase 消除」）。"""
    for e in EXEMPTIONS:
        assert e.eliminated_in in {"P1", "P2", "P3", "P4", "P5", "P6"}, (
            f"exemption {e.file}/{e.business_top} has invalid phase {e.eliminated_in!r}"
        )
        assert e.reason, f"exemption {e.file}/{e.business_top} lacks a reason"
    for e in FIELD_READ_EXEMPTIONS:
        assert e.eliminated_in in {"P1", "P2", "P3", "P4", "P5", "P6"}, (
            f"field-read exemption {e.file}/{e.attr} has invalid phase {e.eliminated_in!r}"
        )
        assert e.reason, f"field-read exemption {e.file}/{e.attr} lacks a reason"


def test_mechanism_paths_exist() -> None:
    """护栏扫描的目标路径必须真实存在，防止路径改名后规则静默失效。"""
    missing = [rel for rel in MECHANISM_PATHS if not (SRC_ROOT / rel).exists()]
    assert not missing, f"mechanism paths missing under {SRC_ROOT}: {missing}"
