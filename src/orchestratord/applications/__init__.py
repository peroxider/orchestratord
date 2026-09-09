"""Business applications built on the orchestration core.

``__init__`` 保持轻量：应用类的绑定经 PEP 562 延迟到首次属性访问
（import 时序契约见 ``applications/issue_pr/__init__.py`` 说明）。
业务 prompt 模板注册由 ``orchestrator`` / ``orchestration_subsystem``
对 ``applications.issue_pr.prompts`` 的顶层 import 触发（DESIGN §4.5/P2）。

应用注册表（DESIGN §6 :420、P5 :471）：cli/api 等入口经
:func:`get_application_class` / :func:`application_specs` 按注册名寻址
应用，而非直接 import 具体组合根类。类绑定同为惰性（函数内 import），
保住 import 时序契约——注册表本体只含 stdlib 数据。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ApplicationSpec",
    "IssueToPrApplication",
    "application_specs",
    "get_application_class",
]


@dataclass(frozen=True)
class ApplicationSpec:
    """注册表条目：应用名 → 组合根类（惰性）与入口（CLI）描述。"""

    name: str
    cli_name: str
    description: str
    class_path: str


_REGISTRY: dict[str, ApplicationSpec] = {
    "issue_pr": ApplicationSpec(
        name="issue_pr",
        cli_name="issue-pr",
        description=(
            "Poll tracker issues, run coding workflows, and synchronize pull requests"
        ),
        class_path="orchestratord.applications.issue_pr.app:IssueToPrApplication",
    ),
    # P6 演练条目（DESIGN §7 :476）：EchoApplication 证明新业务零机制域
    # 改动即可经注册表接入（CLI ``app echo`` / ``app list`` 免费获得寻址）。
    "echo": ApplicationSpec(
        name="echo",
        cli_name="echo",
        description="Minimal demo application — proves the Kernel/Application protocol closes",
        class_path="orchestratord.applications.echo.app:EchoApplication",
    ),
}


def application_specs() -> tuple[ApplicationSpec, ...]:
    """已注册应用条目（``app list`` 与入口寻址的数据源）。"""
    return tuple(_REGISTRY.values())


def get_application_class(name: str) -> type:
    """按注册名解析应用组合根类（惰性 import，保 import 时序契约）。"""
    spec = _REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"unknown application {name!r} — registered: {known}")
    module_path, _, class_name = spec.class_path.partition(":")
    import importlib

    return getattr(importlib.import_module(module_path), class_name)


def __getattr__(name: str):
    if name == "IssueToPrApplication":
        from orchestratord.applications.issue_pr import IssueToPrApplication

        return IssueToPrApplication
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
