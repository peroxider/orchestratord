"""Peer registry integration tests against live Postgres (PR2).

Covers the (workspace_id, orch_id) upsert semantics (D16/D21): fresh
rows start ``pending``, discovery refreshes never resurrect trust
state, ``accepted`` stamps ``accepted_at``, and removal hard-deletes
the row (AC8). Since §6.1 tables carry no ForeignKeys, a bare
``workspace_id`` uuid is enough — referential integrity is app-layer.
Skips when Postgres is unreachable (see conftest).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from orchestratord.db import models as orm
from orchestratord.db.engine import build_session_factory
from orchestratord.domain.auth_token import hash_api_token
from orchestratord.peer.registry import (
    STATUS_ACCEPTED,
    STATUS_PENDING,
    get_peer,
    list_peers,
    remove_peer,
    set_peer_status,
    upsert_peer,
)

pytestmark = pytest.mark.database


async def test_upsert_creates_pending_peer(db) -> None:
    ws = uuid.uuid4()
    peer = await upsert_peer(
        db,
        workspace_id=ws,
        orch_id="orch-B1",
        name="B1",
        url="http://b:9001",
        capabilities=["peer.invoke"],
        card={"name": "B1"},
    )
    assert peer.status == STATUS_PENDING
    assert peer.accepted_at is None
    assert peer.token_id is None
    assert peer.capabilities == ["peer.invoke"]
    assert peer.card == {"name": "B1"}


async def test_upsert_is_keyed_on_workspace_and_orch_id(db) -> None:
    ws = uuid.uuid4()
    await upsert_peer(db, workspace_id=ws, orch_id="orch-B1", name="B1", url="u1")
    again = await upsert_peer(
        db, workspace_id=ws, orch_id="orch-B1", name="B1-renamed", url="u2"
    )
    assert again.name == "B1-renamed"
    assert len(await list_peers(db, ws)) == 1
    # Same orch_id in a different workspace is a distinct relationship.
    other = await upsert_peer(
        db, workspace_id=uuid.uuid4(), orch_id="orch-B1", name="B1", url="u1"
    )
    assert other.workspace_id != ws


async def test_refresh_keeps_trust_state(db) -> None:
    # A re-invite refreshes discovery fields in place but must not
    # silently resurrect a peer's trust state.
    ws = uuid.uuid4()
    peer = await upsert_peer(db, workspace_id=ws, orch_id="orch-B1", name="B1", url="u1")
    await set_peer_status(db, peer, STATUS_ACCEPTED)
    refreshed = await upsert_peer(
        db, workspace_id=ws, orch_id="orch-B1", name="B1", url="u2"
    )
    assert refreshed.status == STATUS_ACCEPTED
    assert refreshed.accepted_at is not None
    assert refreshed.url == "u2"


async def test_accept_stamps_accepted_at(db) -> None:
    ws = uuid.uuid4()
    peer = await upsert_peer(db, workspace_id=ws, orch_id="orch-B1", name="B1", url="u")
    assert peer.accepted_at is None
    await set_peer_status(db, peer, STATUS_ACCEPTED)
    assert peer.accepted_at is not None


async def test_list_peers_filters_by_status(db) -> None:
    ws = uuid.uuid4()
    p1 = await upsert_peer(db, workspace_id=ws, orch_id="orch-B1", name="B1", url="u")
    await upsert_peer(db, workspace_id=ws, orch_id="orch-B2", name="B2", url="u")
    await set_peer_status(db, p1, STATUS_ACCEPTED)
    accepted = await list_peers(db, ws, status=STATUS_ACCEPTED)
    assert [p.orch_id for p in accepted] == ["orch-B1"]
    assert len(await list_peers(db, ws)) == 2


async def test_remove_peer_hard_deletes(db) -> None:
    ws = uuid.uuid4()
    await upsert_peer(db, workspace_id=ws, orch_id="orch-B1", name="B1", url="u")
    assert await remove_peer(db, ws, "orch-B1") is True
    assert await get_peer(db, ws, "orch-B1") is None
    assert await remove_peer(db, ws, "orch-B1") is False


async def test_concurrent_upserts_produce_one_row(db_engine) -> None:
    """PR2 verifier Check 10: two racing invite POSTs must not duplicate
    the registry row. Both callers lose-or-win against
    ``uq_peers_workspace_orch`` and converge on the single row."""
    factory = build_session_factory(db_engine)
    ws = uuid.uuid4()

    async def invite(name: str) -> uuid.UUID:
        async with factory() as session:
            peer = await upsert_peer(
                session, workspace_id=ws, orch_id="orch-RACE", name=name, url="u"
            )
            await session.commit()
            return peer.id

    id_a, id_b = await asyncio.gather(invite("A"), invite("B"))
    assert id_a == id_b
    async with factory() as check:
        peers = await list_peers(check, ws)
        assert [p.orch_id for p in peers] == ["orch-RACE"]
        assert peers[0].id == id_a


async def test_get_peer_sees_single_row_after_racing_invites(db_engine) -> None:
    """The verifier's exact corruption symptom — ``MultipleResultsFound``
    from ``get_peer`` after concurrent invites — must be impossible."""
    factory = build_session_factory(db_engine)
    ws = uuid.uuid4()

    async def invite() -> None:
        async with factory() as session:
            await upsert_peer(
                session, workspace_id=ws, orch_id="orch-RACE2", name="n", url="u"
            )
            await session.commit()

    await asyncio.gather(invite(), invite(), invite())
    async with factory() as check:
        peer = await get_peer(check, ws, "orch-RACE2")
        assert peer is not None


async def test_upsert_race_loser_converges_via_savepoint(
    db_engine, monkeypatch
) -> None:
    """Deterministic replay of the invite race (PR2 verifier repro).

    Plain ``asyncio.gather`` serializes on localhost, so the existing
    race tests above cannot prove convergence. Here both callers are
    held at the initial ``get_peer`` read until each has observed "no
    row", guaranteeing both INSERT and one loses against
    ``uq_peers_workspace_orch``. The loser must converge on the
    winner's row through the savepoint — not raise PendingRollbackError.

    Regression: ``session.add(peer)`` used to run *before*
    ``begin_nested()``, so the loser's failed INSERT stayed pending in
    the outer transaction and the race never converged.
    """
    from orchestratord.peer import registry as peer_registry

    factory = build_session_factory(db_engine)
    ws = uuid.uuid4()
    real_get_peer = peer_registry.get_peer
    read_done = (asyncio.Event(), asyncio.Event())
    reads = 0

    async def gated_get_peer(session, workspace_id, orch_id):
        nonlocal reads
        idx = reads
        reads += 1
        result = await real_get_peer(session, workspace_id, orch_id)
        # Hold both callers until BOTH initial reads have returned, so
        # neither can reach its INSERT while the other is still reading.
        if idx < 2:
            read_done[idx].set()
            await read_done[1 - idx].wait()
        return result

    monkeypatch.setattr(peer_registry, "get_peer", gated_get_peer)

    async def invite() -> uuid.UUID:
        async with factory() as session:
            peer = await upsert_peer(
                session,
                workspace_id=ws,
                orch_id="orch-CONVERGE",
                name="n",
                url="u",
            )
            await session.commit()
            return peer.id

    first, second = await asyncio.wait_for(
        asyncio.gather(invite(), invite()), timeout=15.0
    )
    assert reads == 3  # two initial reads + the loser's re-read
    assert first == second
    async with factory() as check:
        assert [p.orch_id for p in await list_peers(check, ws)] == [
            "orch-CONVERGE"
        ]


# ---------------------------------------------------------------------------
# cli/peer.py registry verbs (AC10) against the live test DB
# ---------------------------------------------------------------------------


def _bind_cli_to_test_db(db_engine, monkeypatch) -> None:
    """Point the CLI's lazily-imported build_session_factory at the test DB."""
    import orchestratord.db.engine as engine_mod

    monkeypatch.setattr(
        engine_mod,
        "build_session_factory",
        lambda engine=None: build_session_factory(db_engine),
    )


async def test_cli_list_accept_remove_roundtrip(
    db, db_engine, monkeypatch, capsys
) -> None:
    from orchestratord.cli.peer import _run_accept, _run_list, _run_remove

    _bind_cli_to_test_db(db_engine, monkeypatch)
    ws = uuid.uuid4()
    peer = await upsert_peer(
        db, workspace_id=ws, orch_id="orch-B1", name="B1", url="http://b:9001"
    )
    await db.commit()

    assert await _run_list(ws, None) == 0
    assert "orch-B1" in capsys.readouterr().out

    # accept: exit 0, token printed once (D15), row marked accepted with
    # a matching auth_tokens row.
    assert await _run_accept(peer.id) == 0
    out = capsys.readouterr().out
    assert "accepted orch-B1" in out
    assert "token (shown once): " in out
    token_line = next(
        line for line in out.splitlines() if line.startswith("token (shown once): ")
    )
    plaintext = token_line.split(": ", 1)[1]
    async with build_session_factory(db_engine)() as check:
        accepted = await get_peer(check, ws, "orch-B1")
        assert accepted is not None and accepted.status == STATUS_ACCEPTED
        assert accepted.token_id is not None
        row = await check.get(orm.AuthToken, accepted.token_id)
        assert row is not None
        assert row.token_hash == hash_api_token(plaintext)

    # remove: exit 0, then the list is empty and a second remove is 404-ish (1).
    assert await _run_remove(ws, "orch-B1") == 0
    capsys.readouterr()
    assert await _run_list(ws, None) == 0
    assert "(no peers)" in capsys.readouterr().out
    assert await _run_remove(ws, "orch-B1") == 1


async def test_cli_accept_unknown_peer_returns_1(
    db_engine, monkeypatch, capsys
) -> None:
    from orchestratord.cli.peer import _run_accept, _run_reject

    _bind_cli_to_test_db(db_engine, monkeypatch)
    ghost = uuid.uuid4()
    assert await _run_accept(ghost) == 1
    assert "peer not found" in capsys.readouterr().err
    assert await _run_reject(ghost) == 1
    assert "peer not found" in capsys.readouterr().err


async def test_cli_reject_deletes_pending_row(db, db_engine, monkeypatch) -> None:
    from orchestratord.cli.peer import _run_reject

    _bind_cli_to_test_db(db_engine, monkeypatch)
    ws = uuid.uuid4()
    peer = await upsert_peer(
        db, workspace_id=ws, orch_id="orch-C3", name="C3", url="u"
    )
    await db.commit()
    assert await _run_reject(peer.id) == 0
    assert await get_peer(db, ws, "orch-C3") is None
