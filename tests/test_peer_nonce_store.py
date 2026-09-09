"""NonceStore (SQLite) replay-guard coverage (DESIGN §7 R2).

First sight records and returns True; an immediate repeat returns
False. Records persist across store instances (a daemon restart must
not lose the replay window), entries expire after their TTL, and
``prune`` removes only expired rows.
"""

from __future__ import annotations

import asyncio

import orchestratord.peer.nonce_store as nonce_store_module
from orchestratord.peer.nonce_store import NonceStore


def _patch_clock(monkeypatch, start: float) -> dict[str, float]:
    clock = {"now": start}
    monkeypatch.setattr(nonce_store_module.time, "time", lambda: clock["now"])
    return clock


async def test_first_sight_true_then_replay_false(tmp_path) -> None:
    store = NonceStore(tmp_path / "nonces.db")
    assert await store.check_and_store("orch-A1", "n-1") is True
    assert await store.check_and_store("orch-A1", "n-1") is False


async def test_same_nonce_different_orch_allowed(tmp_path) -> None:
    store = NonceStore(tmp_path / "nonces.db")
    assert await store.check_and_store("orch-A1", "n-1") is True
    assert await store.check_and_store("orch-B1", "n-1") is True


async def test_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "nonces.db"
    first = NonceStore(path)
    assert await first.check_and_store("orch-A1", "n-1") is True
    # A brand-new instance on the same file (daemon restart) still
    # recognises the nonce.
    second = NonceStore(path)
    assert await second.check_and_store("orch-A1", "n-1") is False


async def test_expired_nonce_can_be_reused(tmp_path, monkeypatch) -> None:
    clock = _patch_clock(monkeypatch, 1_000.0)
    store = NonceStore(tmp_path / "nonces.db")
    assert await store.check_and_store("orch-A1", "n-1", ttl_seconds=10.0) is True
    clock["now"] = 1_005.0  # still inside TTL → replay
    assert await store.check_and_store("orch-A1", "n-1", ttl_seconds=10.0) is False
    clock["now"] = 1_011.0  # TTL elapsed → fresh first sight
    assert await store.check_and_store("orch-A1", "n-1", ttl_seconds=10.0) is True


async def test_prune_removes_only_expired(tmp_path, monkeypatch) -> None:
    clock = _patch_clock(monkeypatch, 1_000.0)
    store = NonceStore(tmp_path / "nonces.db")
    assert await store.check_and_store("orch-A1", "n-exp", ttl_seconds=10.0) is True
    assert await store.check_and_store("orch-A1", "n-live", ttl_seconds=1_000.0) is True
    clock["now"] = 1_020.0
    assert await store.prune() == 1
    # expired entry is gone (and reusable), live entry still blocks
    assert await store.check_and_store("orch-A1", "n-exp", ttl_seconds=10.0) is True
    assert await store.check_and_store("orch-A1", "n-live", ttl_seconds=1_000.0) is False


async def test_concurrent_check_and_store_single_winner(tmp_path) -> None:
    # BEGIN IMMEDIATE serialises writers, so exactly one of the racing
    # check-and-store calls wins.
    store = NonceStore(tmp_path / "nonces.db")
    results = await asyncio.gather(
        *[store.check_and_store("orch-A1", "n-race") for _ in range(8)]
    )
    assert results.count(True) == 1
