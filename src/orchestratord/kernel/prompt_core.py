"""Prompt core — 机制侧通用 prompt 模板与路由（DESIGN §4.5）。

机制层只内置 generic 模板（title/description/priority + CLI 使用指南 +
渲染兜底）与 PromptRouter 扩展点；业务模板（issue/澄清/检视跟进/rebase/
premise 注入）由业务模块（``applications/issue_pr/prompts.py``）通过
:meth:`PromptRouter.register_profile` / :meth:`PromptRouter.register_hook`
注册进来，机制层不 import 任何业务模块。

``GENERIC_DEFAULT_PROMPT`` 与拆分前的 ``_DEFAULT_PROMPT`` 逐字节一致：
P2 验收要求对同一输入渲染结果 byte-identical（tests/test_prompt_snapshot.py
与 scripts/capture_prompt_goldens.py）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

logger = logging.getLogger(__name__)

# Jinja2 environment with strict undefined handling (mirrors Solid's strict_variables)
_jinja_env = Environment(undefined=StrictUndefined)

#: 与拆分前 ``prompt_builder._DEFAULT_PROMPT`` 逐字节一致的通用兜底模板。
#: 其中的 issue 条件分支对非 issue 任务渲染为空，保证任意 kind 的输出
#: 与拆分前完全一致；issue 专属模板由业务侧注册（当前文本一致，
#: P4 起分叉演进）。
GENERIC_DEFAULT_PROMPT = """You are an autonomous software engineering agent.

Task: {{ task.title }}
{% if task.kind == "issue" and task.context and task.context.get("issue_identifier") %}
Issue: {{ task.context.get("issue_identifier") }}
{% endif %}
{% if task.description %}
Description:
{{ task.description }}
{% endif %}
{% if task.priority %}
Priority: {{ task.priority }}
{% endif %}
{% if task.context and task.context.get("issue_state") %}
State: {{ task.context.get("issue_state") }}
{% endif %}

Please analyze the issue, implement the necessary changes, and ensure all tests pass.

## CLI Usage Guidelines
When you need to suggest terminal commands for the user:
- Always use the `orchestratord` CLI entrypoint, NOT `python3 -c` or `PYTHONPATH=`.
- For orchestrator status: `orchestratord server status`
- For issue list: `orchestratord issue list`
- For issue tail: `orchestratord issue tail --id <id>`
- For other commands: use `orchestratord --help`
{% if clarification %}
{{ clarification }}
{% endif %}
"""

#: post-render 钩子签名：(rendered, task_dict, workspace_path) -> rendered。
#: 钩子永不抛出；业务装饰（premise 警告块等）在此挂载。调用顺序 =
#: 注册顺序，注入位置固定在 render() 的 workspace-diff 块之后、
#: previous-attempts 块之前（与拆分前 premise 块位置一致）。
PostRenderHook = Callable[[str, dict[str, Any], "Path | None"], str]


class PromptRouter:
    """按 task.kind 选择模板；业务模板经注册进入，机制层零业务 import。"""

    def __init__(self) -> None:
        self._profiles: dict[str, str] = {}
        self._hooks: list[PostRenderHook] = []

    # -- 模板 profile（task.kind -> 模板文本） ---------------------------
    def register_profile(self, kind: str, template: str) -> None:
        """注册/覆盖某 task.kind 的业务模板（幂等，后写覆盖）。"""
        self._profiles[kind] = template

    def profile_template(self, kind: str | None) -> str | None:
        return self._profiles.get(kind or "")

    # -- post-render 装饰钩子 --------------------------------------------
    def register_hook(self, hook: PostRenderHook) -> None:
        """追加一个 post-render 钩子（幂等：同一函数对象只挂一次）。"""
        if hook not in self._hooks:
            self._hooks.append(hook)

    @property
    def post_render_hooks(self) -> list[PostRenderHook]:
        return list(self._hooks)


_router: PromptRouter | None = None


def get_prompt_router() -> PromptRouter:
    """进程级 PromptRouter 单例（业务注册、机制消费）。"""
    global _router
    if _router is None:
        _router = PromptRouter()
    return _router
