"""Extensible workflow action contract and entry-point registry."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Awaitable, Protocol


@dataclass(frozen=True)
class ActionContext:
    run_context: dict[str, Any]
    workspace_dir: str
    stage_results: dict[int, Any]


@dataclass
class ActionResult:
    success: bool
    outputs: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None


class WorkflowAction(Protocol):
    def __call__(
        self, config: dict[str, Any], context: ActionContext
    ) -> ActionResult | Awaitable[ActionResult]: ...


_ACTIONS: dict[str, WorkflowAction] = {}


def register_action(name: str, action: WorkflowAction) -> None:
    if not name or "." not in name:
        raise ValueError("action names must be namespaced, for example 'job.container'")
    _ACTIONS[name] = action


def _entry_point_actions() -> dict[str, WorkflowAction]:
    try:
        points = entry_points(group="orchestratord.actions")
    except TypeError:
        points = entry_points().get("orchestratord.actions", [])
    result: dict[str, WorkflowAction] = {}
    for point in points:
        result[point.name] = point.load()
    return result


def resolve_action(name: str) -> WorkflowAction:
    action = _ACTIONS.get(name) or _entry_point_actions().get(name)
    if action is None:
        raise LookupError(f"workflow action {name!r} is not registered")
    return action


async def execute_action(
    name: str, config: dict[str, Any], context: ActionContext
) -> ActionResult:
    value = resolve_action(name)(config, context)
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, ActionResult):
        raise TypeError(f"workflow action {name!r} returned {type(value).__name__}, expected ActionResult")
    return value
