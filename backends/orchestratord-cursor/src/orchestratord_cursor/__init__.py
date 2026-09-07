"""orchestratord-cursor — Cursor CLI backend.

Wraps the ``cursor-agent`` binary as a spawn-per-turn Cli backend. The
CLI's ``--output-format stream-json`` stream is translated wire-level,
ported from the multica Go reference.
"""

from orchestratord_cursor.backend import CursorBackend

__all__ = ["CursorBackend"]