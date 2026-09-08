"""orchestratord-kiro-cli — AWS Kiro CLI backend.

Wraps the ``kiro`` binary as a spawn-per-turn Cli backend. §8.1 marks
the kiro event stream as "not yet exercised", so this backend is
intentionally conservative: no streaming-deltas claim until the wire
format is exercised in-tree.
"""

from orchestratord_kiro_cli.backend import KiroBackend

__all__ = ["KiroBackend"]
