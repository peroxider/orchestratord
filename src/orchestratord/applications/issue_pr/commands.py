"""Issue→PR control-command handlers.

Handlers live behind the Application command registry.  The private legacy
callbacks are temporary implementation adapters and are intentionally not
part of the Kernel control surface.
"""

from __future__ import annotations

from typing import Any


class IssuePrCommands:
    """Named business command handlers for the issue→PR application."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def _delegate(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return getattr(self.host, f"_legacy{name}")(*args, **kwargs)

    def _handle_rebase_control(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate("_handle_rebase_control", *args, **kwargs)

    def _handle_review_followup_control(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate("_handle_review_followup_control", *args, **kwargs)

    def _handle_review_retry_control(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate("_handle_review_retry_control", *args, **kwargs)

    def _handle_review_approve_control(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate("_handle_review_approve_control", *args, **kwargs)

    def _handle_retry_control(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate("_handle_retry_control", *args, **kwargs)

    def _handle_followup_control(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate("_handle_followup_control", *args, **kwargs)


__all__ = ["IssuePrCommands"]
