"""Reliability facade.

Re-exports :class:`ReliabilityStore` (the file-based backend in
``store.py``) and groups the reliability-related helpers. The backend
interface is intentionally narrow so a SQLite/Postgres backend can
slot in later (P6+) without touching callers.
"""

from __future__ import annotations

from .config import ReliabilityConfig
from .store import ReliabilityStore

__all__ = ["ReliabilityConfig", "ReliabilityStore"]
