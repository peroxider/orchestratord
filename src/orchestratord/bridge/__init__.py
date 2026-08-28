"""orchestratord.bridge — worker-process lifecycle for Protocol backends.

The bridge package owns the management of long-lived agent worker
subprocesses (currently the ``codex app-server`` JSON-RPC worker used by
the ``orchestratord-codex`` backend).
"""

from .worker import WorkerManager

__all__ = ["WorkerManager"]
