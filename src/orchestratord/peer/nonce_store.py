"""SQLite-backed nonce replay guard for ``peer/1`` (DESIGN §7 R2).

Nonces are keyed by ``(orch_id, nonce)`` with a TTL of twice the HMAC
timestamp window (±60s), so any replay that passes the window check is
still caught here, and entries expire on their own — no background
sweeper is required for correctness (``prune`` only reclaims space).

Phase 1 runs single-process: stdlib ``sqlite3`` via ``asyncio.to_thread``,
one connection per operation in explicit-transaction mode
(``BEGIN IMMEDIATE``) so a second process on a shared file cannot race
a check-and-store. A Redis SETNX backend can slot in behind the same
async surface when multi-instance deployments arrive (PR3).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

# 2× the ±60s HMAC window (hmac_sig.TIMESTAMP_WINDOW_SECONDS): a nonce
# stays recorded for as long as its frame could pass the window check.
DEFAULT_NONCE_TTL_SECONDS = 120.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_nonces (
    orch_id    TEXT NOT NULL,
    nonce      TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (orch_id, nonce)
)
"""


class NonceStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
        finally:
            conn.close()

    async def check_and_store(
        self,
        orch_id: str,
        nonce: str,
        *,
        ttl_seconds: float = DEFAULT_NONCE_TTL_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Return True (and record the nonce) on first sight; False on replay.

        An expired record for the same key is replaced with a fresh one
        (INSERT OR REPLACE), so a genuinely new frame whose nonce
        collides with a long-gone entry is not falsely rejected.
        """
        now = time.time() if now is None else now
        return await asyncio.to_thread(
            self._check_and_store_sync, orch_id, nonce, ttl_seconds, now
        )

    async def prune(self, *, now: float | None = None) -> int:
        """Delete expired records; return how many rows were removed."""
        now = time.time() if now is None else now
        return await asyncio.to_thread(self._prune_sync, now)

    # -- sync helpers (run in a worker thread) --

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0, isolation_level=None)

    def _check_and_store_sync(
        self, orch_id: str, nonce: str, ttl_seconds: float, now: float
    ) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT 1 FROM peer_nonces"
                " WHERE orch_id = ? AND nonce = ? AND expires_at > ?",
                (orch_id, nonce, now),
            ).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT OR REPLACE INTO peer_nonces (orch_id, nonce, expires_at)"
                " VALUES (?, ?, ?)",
                (orch_id, nonce, now + ttl_seconds),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _prune_sync(self, now: float) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM peer_nonces WHERE expires_at <= ?", (now,)
            )
            conn.execute("COMMIT")
            return cur.rowcount
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


class RedisNonceStore:
    """Redis ``SET NX EX`` nonce guard for multi-instance deployments (R2).

    Same async surface as :class:`NonceStore` so
    :func:`orchestratord.peer.hmac_sig.verify` accepts either. First
    sight maps to a successful ``SET ... NX EX``; a replay loses the
    race and gets ``False``. Redis owns expiry, so ``prune`` is a no-op
    and the ``now`` clock parameter exists only for surface parity.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        *,
        key_prefix: str = "orch:peer:nonce",
        redis_client: Any | None = None,
    ) -> None:
        if redis_client is not None:
            self._redis = redis_client
        else:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(redis_url)
        self._prefix = key_prefix

    def _key(self, orch_id: str, nonce: str) -> str:
        return f"{self._prefix}:{orch_id}:{nonce}"

    async def check_and_store(
        self,
        orch_id: str,
        nonce: str,
        *,
        ttl_seconds: float = DEFAULT_NONCE_TTL_SECONDS,
        now: float | None = None,
    ) -> bool:
        # Redis EX is whole seconds; a sub-second TTL would round to 0
        # and be rejected by the server, so clamp to at least 1s.
        ttl = max(1, round(ttl_seconds))
        stored = await self._redis.set(
            self._key(orch_id, nonce), 1, nx=True, ex=ttl
        )
        return bool(stored)

    async def prune(self, *, now: float | None = None) -> int:
        return 0  # Redis expires the keys on its own

    async def aclose(self) -> None:
        await self._redis.aclose()
