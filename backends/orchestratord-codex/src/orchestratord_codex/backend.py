"""CodexBackend — Cli/SdkProcess backend wrapping the ``codex`` binary.

The backend probes ``codex app-server --help`` at construction time:

* exit code 0 → uses :class:`CodexAppServerSession` (SdkProcess — adds
  ``streaming_deltas + interrupt + approval_hooks``)
* exit code non-0 / binary missing → uses :class:`CodexSession` (Cli —
  only ``resumable + parallel_sessions``)

The runtime is cached on the backend instance and re-used for every
``create_session`` call; we do not probe per-session. The CLI path is
the fallback for older ``codex`` builds that lack ``app-server``.

The ``Capabilities:`` line above lists the **union** of both runtimes'
bit sets so the drift detector accepts either (see Scheme D §4.7).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Literal

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession

from orchestratord_codex.app_server_session import CodexAppServerSession
from orchestratord_codex.session import CodexSession

logger = logging.getLogger(__name__)

_RuntimeKind = Literal["cli", "as"]


async def _probe_app_server() -> bool:
    """Return True iff ``codex app-server --help`` exits 0.

    Returns False on ``FileNotFoundError`` (no codex binary on PATH).
    The probe is wrapped in :func:`asyncio.wait_for` so a hung
    subprocess cannot stall backend construction; see Scheme A §1.7.
    """
    binary = shutil.which("codex")
    if binary is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            "--help",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("codex app-server probe could not start: %s", exc)
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    return proc.returncode == 0


def _detect_runtime() -> _RuntimeKind:
    """Synchronous wrapper around :func:`_probe_app_server`.

    Used at backend construction time. Falls back to ``cli`` on any
    exception (the historical safe default).
    """
    try:
        # ``asyncio.run()`` cannot be nested.  Backend construction may
        # happen inside the daemon's event loop, in which case probing is
        # deferred to an explicit preflight and the safe CLI runtime is used.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            probe = _probe_app_server()
            try:
                return "as" if asyncio.run(probe) else "cli"
            except Exception:
                # A mocked or broken event-loop runner can reject before
                # consuming the coroutine; close it to avoid an unawaited
                # coroutine warning during error fallback.
                probe.close()
                raise
        logger.debug("codex runtime probe deferred inside a running event loop")
        return "cli"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("codex runtime probe failed, defaulting to cli: %s", exc)
        return "cli"


class CodexBackend:
    """Cli/SdkProcess backend for the ``codex`` binary.

    Runtime selection order (DESIGN_backends_hardening.md §1.2):

    1. explicit ``prefer={cli,as}`` constructor argument (highest priority)
    2. ``ORCHESTRATORD_CODEX_PREFER`` environment variable (``cli``/``as``)
    3. runtime probe ``codex app-server --help`` (default)

    The :pyattr:`family` attribute and capability bits reflect the
    selected runtime; see Scheme A §1.4.
    """

    name = "codex"
    display_name = "Codex (Cli/As)"

    def __init__(self, prefer: _RuntimeKind | None = None) -> None:
        if prefer is not None:
            if prefer not in ("cli", "as"):
                raise ValueError(f"prefer must be 'cli' or 'as', got {prefer!r}")
            self._runtime: _RuntimeKind = prefer
        else:
            env_prefer = os.environ.get("ORCHESTRATORD_CODEX_PREFER")
            if env_prefer in ("cli", "as"):
                logger.info("codex runtime forced by env: %s", env_prefer)
                self._runtime = env_prefer  # type: ignore[assignment]
            else:
                self._runtime = _detect_runtime()
        self._sessions: list[AgentSession] = []

    @property
    def runtime(self) -> _RuntimeKind:
        """Return the selected runtime: ``"cli"`` or ``"as"``.

        Exposed for diagnostics and the ``--prefer`` override.
        """
        return self._runtime

    def preflight(self, spec: SessionSpec) -> None:  # noqa: ARG002
        """Verify the Codex executable is installed before daemon startup."""
        if shutil.which("codex") is None:
            raise RuntimeError(
                "codex executable was not found on PATH. Install Codex and "
                "complete its own authentication before using this backend."
            )

    def capabilities(self) -> BackendCapabilities:
        if self._runtime == "as":
            return BackendCapabilities(
                streaming_deltas=True,
                resumable=False,
                interrupt=True,
                approval_hooks=True,
                parallel_sessions=True,
                cost_reporting=False,
                tool_filtering=False,
                takeover=False,
                # ADR-003: AppServer backend exposes session/load MCP
                # probe (see app_server_session.py:probe_resume).
                resume_detection=True,
            )
        return BackendCapabilities(
            streaming_deltas=False,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            # ADR-003: CLI backend has no cross-process probe path.
            resume_detection=False,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        cls = CodexAppServerSession if self._runtime == "as" else CodexSession
        session = cls(spec)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._sessions.clear()
