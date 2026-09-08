"""Control only the process tree owned by a single backend invocation."""

import logging
from typing import Callable

import psutil

logger = logging.getLogger(__name__)


class ProcessTree:
    """PID-reuse-safe handles, including tools that create new sessions.

    Freeze parents before enumerating children so they cannot keep spawning
    descendants during the traversal. A process group alone is insufficient:
    agent shell tools and MCP workers may use setsid().
    """

    def __init__(self, pid: int):
        self.root = psutil.Process(pid)
        self.frozen: list[psutil.Process] = []
        self._owned = [self.root]

    def remember_descendants(self) -> None:
        """Retain identity-safe handles before a graceful parent shutdown."""
        try:
            children = self.root.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        for process in children:
            if process not in self._owned:
                self._owned.append(process)

    def pause(self) -> None:
        if self.frozen:
            return
        pending = list(self._owned)
        seen = set()
        try:
            while pending:
                process = pending.pop(0)
                if process.pid in seen:
                    continue
                seen.add(process.pid)
                try:
                    process.suspend()
                    self.frozen.append(process)
                    children = process.children()
                    pending.extend(children)
                    for child in children:
                        if child not in self._owned:
                            self._owned.append(child)
                except psutil.NoSuchProcess:
                    pass
            if not self.frozen:
                raise RuntimeError("No live agent process to pause")
        except Exception:
            self.resume()
            raise

    def resume(self) -> None:
        # Children first: the root may immediately dispatch more tool work.
        failed = []
        for process in reversed(self.frozen):
            try:
                process.resume()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                failed.append(process)
        self.frozen = list(reversed(failed))
        if failed:
            raise RuntimeError("Unable to resume every agent child process")

    def kill(self) -> None:
        """Freeze then kill descendants and root, including paused workers.

        SIGKILL does not require resuming a stopped process. This avoids
        giving a tool another chance to write or spawn during operator stop.
        psutil signal methods guard against PID reuse.
        """
        if not self.frozen:
            try:
                self.pause()
            except (psutil.NoSuchProcess, RuntimeError):
                if not any(process.is_running() for process in self._owned):
                    return
                raise
        failed = []
        for process in reversed(self.frozen):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                failed.append(process)
        self.frozen = list(reversed(failed))
        if failed:
            raise RuntimeError("Unable to stop every agent child process")


class TurnProcessControl:
    """Operator-control adapter for backends that spawn one child per turn.

    ProcessTree is built around a PID known up front; per-turn backends
    (e.g. ``claude -p``) only have a live PID *during* a turn. This
    wrapper re-resolves the tree from ``pid_provider`` at each operation,
    so pause/resume/kill always target the current turn's subprocess and
    raise between turns instead of silently no-opping.

    Duck-types the ProcessTree surface the live-registry consumers use
    (``pause``/``resume``/``kill``).
    """

    def __init__(
        self,
        pid_provider: Callable[[], int | None],
        stop_command: Callable[[], None] | None = None,
    ) -> None:
        self._pid_provider = pid_provider
        self._stop_command = stop_command
        self._tree: ProcessTree | None = None

    def _live_tree(self) -> ProcessTree:
        pid = self._pid_provider()
        if pid is None:
            raise RuntimeError(
                "No live agent process to control (between turns)"
            )
        return ProcessTree(pid)

    def pause(self) -> None:
        if self._tree is not None:
            return
        self._tree = self._live_tree()
        self._tree.pause()

    def resume(self) -> None:
        if self._tree is None:
            return
        self._tree.resume()
        self._tree = None

    def kill(self) -> None:
        """Kill the current turn's tree; between turns, only fire stop.

        An absent child means the turn already ended on its own — there
        is nothing to signal, but the operator's stop must still reach
        the orchestrator (via the control-socket command in
        ``stop_command``). Paused trees kill from the frozen handles.
        """
        try:
            if self._tree is not None:
                self._tree.kill()
            else:
                pid = self._pid_provider()
                if pid is not None:
                    ProcessTree(pid).kill()
        finally:
            self._tree = None
            if self._stop_command is not None:
                try:
                    self._stop_command()
                except Exception:
                    logger.exception(
                        "post-kill stop command failed; the run ends via "
                        "the backend's own exit path instead"
                    )
