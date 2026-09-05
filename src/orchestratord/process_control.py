"""Control only the process tree owned by a single backend invocation."""

import psutil


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
