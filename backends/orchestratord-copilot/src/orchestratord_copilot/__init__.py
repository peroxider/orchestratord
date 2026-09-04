"""orchestratord-copilot — GitHub Copilot CLI backend.

Wraps the ``copilot`` binary as a spawn-per-turn Cli backend. §8.1
marks the copilot event stream as "needs experimentation", so this
backend is intentionally conservative: no streaming-deltas claim
until the wire format is exercised in-tree.
"""

from orchestratord_copilot.backend import CopilotBackend

__all__ = ["CopilotBackend"]