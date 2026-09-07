"""orchestratord-copilot — GitHub Copilot CLI backend.

Wraps the ``copilot`` binary as a spawn-per-turn Cli backend. The CLI's
``--output-format json`` JSONL stream (``assistant.message_delta`` text
fragments) is translated wire-level, ported from the multica Go
reference — hence the ``streaming_deltas`` capability claim.
"""

from orchestratord_copilot.backend import CopilotBackend

__all__ = ["CopilotBackend"]