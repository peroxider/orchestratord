"""ClaudeBackend — CLI backend wrapping the ``claude -p`` invocation.

Per-turn spawn model (DESIGN_backends_hardening.md §3):

* Each :py:meth:`create_session` returns a :class:`ClaudeSession`
  that holds the spec but does not launch the CLI until the first
  :py:meth:`ClaudeSession.send` call. This keeps :py:meth:`preflight`
  cheap (just binary resolution) and lets the orchestrator enforce
  timeouts around the spawn → first-event window separately from
  the per-turn wallclock.
* Resolution order: ``spec.runtime_bin`` → ``$CLAUDE_BIN`` → first of
  ``claude`` / ``ccb`` reachable via ``shutil.which``.
* Streaming is enabled by ``--output-format stream-json --verbose``;
  we deliberately pass ``--dangerously-skip-permissions`` because
  the daemon runs unattended and a permission prompt would hang
  the session indefinitely.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_claude.session import ClaudeSession


# Executable candidates probed on $PATH in order. ``claude`` is the
# upstream Anthropic CLI; ``ccb`` is the open-source fork under the
# ``claude-code-best`` moniker. Both speak the same stream-json
# protocol, so the rest of the adapter does not care which one we got.
_CLAUDE_BINARY_CANDIDATES: tuple[str, ...] = ("claude", "ccb")


def _resolve_claude_binary(spec: SessionSpec) -> str:
    """Return the absolute path of the ``claude`` / ``ccb`` binary.

    Order:
      1. ``spec.runtime_bin`` if non-empty (explicit operator override).
      2. ``$CLAUDE_BIN`` env var if non-empty.
      3. The first of ``claude`` / ``ccb`` reachable via ``shutil.which``.
    """
    explicit = (spec.runtime_bin or "").strip()
    if explicit:
        return explicit
    env_bin = (os.environ.get("CLAUDE_BIN") or "").strip()
    if env_bin:
        return env_bin
    for candidate in _CLAUDE_BINARY_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError(
        "claude backend: no executable found. Install `claude` (Anthropic CLI) "
        "or `ccb` (open-source fork), or set `agent.runtime_bin` / "
        "`CLAUDE_BIN` to an absolute path."
    )


class ClaudeBackend:
    """CLI backend that spawns the ``claude -p`` subprocess per turn."""

    name = "claude"
    display_name = "Claude Code (CLI: claude / ccb)"

    def __init__(self) -> None:
        self._sessions: list[ClaudeSession] = []

    # ------------------------------------------------------------------
    # AgentBackend protocol
    # ------------------------------------------------------------------

    def preflight(self, spec: SessionSpec) -> None:
        """Verify the claude binary is on $PATH and runnable."""
        binary = _resolve_claude_binary(spec)
        if not Path(binary).exists():
            raise RuntimeError(
                f"claude backend: resolved binary does not exist: {binary}"
            )

    def capabilities(self) -> BackendCapabilities:
        # Mirrors CLAUDE_DESCRIPTOR.capabilities exactly — backend_registry's
        # strict-mode guard raises BackendMismatchError if the two drift.
        return BackendCapabilities(
            streaming_deltas=True,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=False,
            takeover=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        binary = _resolve_claude_binary(spec)
        session = ClaudeSession(spec, binary=binary)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        for s in self._sessions:
            try:
                close = getattr(s, "close_sync", None) or getattr(s, "close", None)
                if close is not None:
                    close()
            except Exception:
                pass
        self._sessions.clear()
