"""orchestratord-reasonix — Reasonix CLI backend.

Wraps the ``reasonix`` binary as a spawn-per-turn Cli backend. §8.1 marks
the reasonix event stream as "not yet exercised", so this backend is
intentionally conservative: no streaming-deltas claim until the wire
format is exercised in-tree.
"""

from orchestratord_reasonix.backend import ReasonixBackend

__all__ = ["ReasonixBackend"]
