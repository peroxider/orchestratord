"""Synchronous terminal boundary for the asynchronous application services."""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Callable
from typing import Any

from .models import CommandContext, CommandOutput, CommandResult


class TerminalOutput(CommandOutput):
    """Render at the terminal boundary as work proceeds (before prompts)."""

    def __init__(self) -> None:
        super().__init__(limit=None)

    def write(
        self, *values: object, sep: str = " ", end: str = "\n", error: bool = False
    ) -> None:
        print(*values, sep=sep, end=end, file=sys.stderr if error else sys.stdout)


def render(result: CommandResult) -> int:
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.exit_code


def invoke(
    operation: Callable, context: CommandContext, *args: Any, **kwargs: Any
) -> Any:
    """Adapt legacy CLI helpers without importing CLI code into the service."""
    context.output = TerminalOutput()
    try:
        result = operation(context, *args, **kwargs)
        return asyncio.run(result) if inspect.isawaitable(result) else result
    finally:
        render(CommandResult(0, context.output.stdout, context.output.stderr))


async def invoke_async(
    operation: Callable, context: CommandContext, *args: Any, **kwargs: Any
) -> Any:
    context.output = TerminalOutput()
    try:
        result = operation(context, *args, **kwargs)
        return await result if inspect.isawaitable(result) else result
    finally:
        render(CommandResult(0, context.output.stdout, context.output.stderr))
