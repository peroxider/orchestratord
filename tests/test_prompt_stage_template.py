"""Prompt template must tolerate stage tasks.

``run start`` synthesizes stage tasks whose context has
``stage_id``/``phase`` but no ``issue_state``. The default prompt
template referenced ``task.context.issue_state`` directly, and under
``StrictUndefined`` that raised UndefinedError on every stage render —
logged as "Template render error" noise (the fallback then re-raised
the same error unless the caller was lucky).
"""

from __future__ import annotations

import logging

from orchestratord.agent.task import AgentTask
from orchestratord.prompt_builder import PromptBuilder


def _stage_task() -> AgentTask:
    return AgentTask(
        id="stage-01",
        kind="workflow_stage",
        title="[build] stage one",
        description="do the thing",
        context={"stage_id": 1, "phase": "build"},
    )


def test_render_stage_task_without_issue_state_is_clean(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="orchestratord.prompt_builder"):
        prompt = PromptBuilder.render(_stage_task())

    assert prompt, "render must produce a prompt"
    assert "do the thing" in prompt
    errors = [r for r in caplog.records if "Template render error" in r.message]
    assert errors == [], "rendering a stage task must not log template errors"


def test_render_issue_task_still_includes_state() -> None:
    task = AgentTask(
        id="7",
        kind="issue",
        title="fix bug",
        description="desc",
        context={"issue_state": "open"},
    )
    prompt = PromptBuilder.render(task)
    assert "State: open" in prompt
