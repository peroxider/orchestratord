"""机制控制命令的最小分发器。

业务命令（review/rebase/follow-up）不属于此模块；它们由 Application
注册。这里仅提供 pause/resume/stop/takeover 等通用命令的可替换 seam。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable


class KernelControl:
    """Dispatch named mechanism commands without knowing their payload shape."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[str], Any | Awaitable[Any]]] = {}

    def register(self, name: str, handler: Callable[[str], Any | Awaitable[Any]]) -> None:
        self._handlers[name] = handler

    async def dispatch(self, name: str, payload: str = "") -> Any:
        handler = self._handlers.get(name)
        if handler is None:
            return False
        result = handler(payload)
        if hasattr(result, "__await__"):
            return await result
        return result


__all__ = ["KernelControl"]
