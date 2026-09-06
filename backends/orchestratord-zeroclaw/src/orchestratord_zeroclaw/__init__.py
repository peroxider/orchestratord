"""orchestratord-zeroclaw — ZeroClaw CLI backend.

Wraps the ``zeroclaw`` binary as a spawn-per-turn Cli backend. §8.1 marks
the zeroclaw event stream as "not yet exercised", so this backend is
intentionally conservative: no streaming-deltas claim until the wire
format is exercised in-tree.
"""

from orchestratord_zeroclaw.backend import ZeroclawBackend

__all__ = ["ZeroclawBackend"]
