"""orchestratord-cursor — Cursor CLI backend.

Wraps the ``cursor-agent`` binary as a spawn-per-turn Cli backend.
"""

from orchestratord_cursor.backend import CursorBackend

__all__ = ["CursorBackend"]