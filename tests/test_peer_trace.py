"""Cross-daemon trace correlation tests (telemetry 收尾).

Covers the three seams the trace id rides:

* :mod:`orchestratord.peer.trace` helpers (resolution order, header
  lookup, contextvar binding),
* ``PeerClient.invoke`` — stamps ``headers["x-trace-id"]`` (explicit →
  ambient contextvar → fresh),
* the two inbound surfaces echo it back — RESULT frame headers on the
  frame path, ``X-Trace-Id`` response header on the REST invoke path.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from orchestratord.api.app import create_app
from orchestratord.api.routers import peer_frame
from orchestratord.api.routers.peer import reset_dispatcher
from orchestratord.db import models as orm
from orchestratord.domain.auth_token import hash_api_token, issue_api_token
from orchestratord.peer.client import PeerClient
from orchestratord.peer.hmac_sig import sign
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType
from orchestratord.peer.registry import upsert_peer
from orchestratord.peer.trace import (
    TRACE_HEADER,
    current_trace_id,
    new_trace_id,
    reset_current_trace_id,
    resolve_trace_id,
    set_current_trace_id,
    trace_id_from_headers,
)


# -- helpers (trace.py) --


def test_trace_helpers_resolution_order() -> None:
    assert len(new_trace_id()) == 32
    assert new_trace_id() != new_trace_id()
    # explicit > ambient > fresh
    token = set_current_trace_id("ambient-id")
    try:
        assert resolve_trace_id("explicit") == "explicit"
        assert resolve_trace_id() == "ambient-id"
    finally:
        reset_current_trace_id(token)
    assert current_trace_id() is None
    assert resolve_trace_id() not in ("explicit", "ambient-id")
    assert len(resolve_trace_id()) == 32


def test_trace_header_lookup_is_case_insensitive() -> None:
    assert trace_id_from_headers({"X-Trace-Id": "abc"}) == "abc"
    assert trace_id_from_headers({"X-TRACE-ID": "abc"}) == "abc"
    assert trace_id_from_headers({"x-trace-id": "abc"}) == "abc"
    assert trace_id_from_headers({"other": "abc"}) is None
    assert trace_id_from_headers(None) is None
    assert trace_id_from_headers({"x-trace-id": ""}) is None


# -- PeerClient.invoke stamps the trace header --


def _make_client() -> PeerClient:
    async def _no_transport():  # pragma: no cover — must never be called
        raise AssertionError("invoke must not open a transport")

    return PeerClient(
        orch_id="orch-A1",
        token="tok",
        transport_factory=_no_transport,
        nonce_store=NonceStore("/tmp/peer-trace-nonces.db"),
        request_timeout=5,
    )


@pytest.mark.asyncio
async def test_client_invoke_stamps_explicit_trace_id() -> None:
    client = _make_client()
    captured: list[PeerFrame] = []

    async def fake_send(
        frame: PeerFrame, *, end_of_batch: bool = False
    ) -> None:
        captured.append(frame)
        fut = client._pending[frame.request_id]
        fut.set_result(
            PeerFrame.result(
                orch_id="remote", request_id=frame.request_id,
                status=200, body={}, msg_id=frame.msg_id,
            )
        )

    client._send_or_fail = fake_send  # type: ignore[method-assign]
    await client.invoke("GET /api/x", {}, headers={"x-trace-id": "trace-1"})
    assert captured[0].headers is not None
    assert captured[0].headers[TRACE_HEADER] == "trace-1"


@pytest.mark.asyncio
async def test_client_invoke_reuses_ambient_or_fresh_trace_id() -> None:
    client = _make_client()
    captured: list[PeerFrame] = []

    async def fake_send(
        frame: PeerFrame, *, end_of_batch: bool = False
    ) -> None:
        captured.append(frame)
        fut = client._pending[frame.request_id]
        fut.set_result(
            PeerFrame.result(
                orch_id="remote", request_id=frame.request_id,
                status=200, body={}, msg_id=frame.msg_id,
            )
        )

    client._send_or_fail = fake_send  # type: ignore[method-assign]
    token = set_current_trace_id("ambient-trace")
    try:
        await client.invoke("GET /api/x", {})
        assert captured[-1].headers is not None
        assert captured[-1].headers[TRACE_HEADER] == "ambient-trace"
    finally:
        reset_current_trace_id(token)

    await client.invoke("GET /api/x", {})
    fresh = captured[-1].headers[TRACE_HEADER]
    assert fresh != "ambient-trace" and len(fresh) == 32


# -- inbound surfaces (TestClient harness mirroring test_peer_frame_router) --


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ORCHESTRATORD_PEER_NONCE_PATH", str(tmp_path / "nonces.db")
    )
    peer_frame.reset_nonce_store()
    reset_dispatcher()
    from orchestratord.api.deps import reset_peer_rate_bucket
    from orchestratord.api.realtime import reset_broker

    reset_broker()
    reset_peer_rate_bucket()
    yield create_app()
    reset_peer_rate_bucket()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


async def _build_accepted_peer(tmp_path):
    from orchestratord.db.engine import build_engine, create_schema

    db_path = tmp_path / "peer_trace.db"
    import os

    os.environ["ORCHESTRATORD_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    engine = build_engine()
    await create_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        peer = await upsert_peer(
            session,
            workspace_id=uuid.uuid4(),
            orch_id="orch-REMOTE",
            name="Remote",
            url="http://r:9001",
            capabilities=["peer.invoke"],
        )
        plaintext, token_hash = issue_api_token()
        token_row = orm.AuthToken(
            id=uuid.uuid4(),
            workspace_id=peer.workspace_id,
            name=f"peer:{peer.orch_id}",
            token_hash=hash_api_token(plaintext),
            scopes=["peer.invoke"],
            expires_at=None,
            created_at=datetime.now(UTC),
        )
        session.add(token_row)
        await session.flush()
        peer.token_id = token_row.id
        await session.commit()
        await session.refresh(peer)
    await engine.dispose()
    return peer, plaintext


def test_frame_invoke_echoes_trace_id_in_result_headers(
    client, tmp_path
) -> None:
    try:
        peer, plaintext = asyncio.run(_build_accepted_peer(tmp_path))
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    hello = PeerFrame.hello(orch_id=peer.orch_id)
    sign(hello, plaintext)
    invoke = PeerFrame.invoke(
        orch_id=peer.orch_id,
        request_id="req-trace",
        method="peer.tasks.create",
        body={},
        headers={"X-Trace-Id": "trace-frame-42"},
    )
    sign(invoke, plaintext)
    body = hello.encode() + invoke.encode()

    with client.stream(
        "POST",
        "/peer/v1/stream",
        content=body,
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": f"Bearer {plaintext}",
            "X-Peer-Orchestrator-Id": peer.orch_id,
        },
    ) as resp:
        assert resp.status_code == 200
        lines = [ln for ln in resp.iter_lines() if ln.strip()]
    results = [
        PeerFrame.decode(ln)
        for ln in lines
        if PeerFrame.decode(ln).type is PeerFrameType.RESULT
    ]
    assert results, "expected a RESULT frame"
    assert results[0].headers is not None
    assert results[0].headers.get(TRACE_HEADER) == "trace-frame-42"


def test_rest_invoke_echoes_x_trace_id_response_header(
    client, tmp_path
) -> None:
    try:
        peer, plaintext = asyncio.run(_build_accepted_peer(tmp_path))
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    resp = client.post(
        f"/api/peer/peers/{peer.orch_id}/invoke",
        json={
            "method": "GET /api/workspaces/{workspace_id}/sessions",
            "body": {"workspace_id": str(peer.workspace_id)},
            "msg_id": "m-trace-1",
        },
        headers={
            "Authorization": f"Bearer {plaintext}",
            "X-Peer-Orchestrator-Id": peer.orch_id,
            "X-Trace-Id": "trace-rest-7",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-trace-id") == "trace-rest-7"
