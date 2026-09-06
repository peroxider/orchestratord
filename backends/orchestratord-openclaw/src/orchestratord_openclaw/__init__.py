"""orchestratord-openclaw — OpenClaw CLI backend.

Wraps the ``openclaw`` binary as a spawn-per-turn Cli backend. §8.1 marks
openclaw's native protocol as HTTP (``openclaw agent --local`` vs Gateway
routing); that wire path is deferred, so this backend is intentionally
conservative: it buffers stdout as a single TEXT event and does not yet
claim ``streaming_deltas``.
"""

from orchestratord_openclaw.backend import OpenclawBackend

__all__ = ["OpenclawBackend"]
