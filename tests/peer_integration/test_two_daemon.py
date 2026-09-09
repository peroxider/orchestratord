"""Cross-process federation: two real ``orchestratord serve`` daemons.

DESIGN §10 PR6 row — two daemons, one shared Redis, each with its own
scratch Postgres database and its own ``HOME`` (so each gets its own
``~/.orchestratord`` orch_id, pre-pinned to a fixed value so the R10
``ORCHESTRATORD_PEER_TRUST`` whitelist matches deterministically).

Phase-1 boundary, encoded here on purpose: ``serve`` does not bind the
peer/1 frame listener yet (no network ``FrameTransport`` exists), so
cross-daemon traffic is the PR5 HTTP surface driven the way Phase B's
outbound layer will drive it — bearer token from the §5 invite
handshake, ``X-Peer-Orchestrator-Id`` identifying the caller. The flows
under test:

1. **Remote INVOKE (B→A)** — B's token appends a message into an A-side
   session; D18 replay returns ``duplicate=True`` without a second row;
   D19 ordering gap flags ``out_of_order`` in the response and the audit
   row; the audit trail carries ``invited_by_orch_id``.
2. **Cross-daemon session + auto-schedule (A→B)** — ``POST
   /api/peer/peers/{orch}/sessions`` lands a ``pending`` session in B's
   DB and B's real chat-dispatcher claim loop (§6.1d) takes it to a
   terminal status. The backend name is deliberately unresolvable so
   the terminal status is ``failed`` — proving the turn ran without
   needing a real agent.
3. **SSE over the Redis relay (AC7/D22)** — a frame published on A's
   D22 channel lands in B's broker via ``PeerEventRelay`` and is served
   to a subscriber on B's ``peer.*``-only SSE endpoint.

Skips cleanly when Postgres, Redis, or ``uv`` is unavailable.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import httpx
import pytest
import pytest_asyncio

from orchestratord.db.engine import build_engine, create_schema
from orchestratord.peer.redis_relay import peer_channel
from orchestratord.seed import DEFAULT_WORKSPACE_SLUG

pytestmark = pytest.mark.database

_ADMIN_DSN = "postgresql://multica:multica@127.0.0.1:5432/multica"
_DSN_TEMPLATE = "postgresql://multica:multica@127.0.0.1:5432/{name}"
_ALCHEMY_DSN_TEMPLATE = "postgresql+asyncpg://multica:multica@127.0.0.1:5432/{name}"
_REDIS_URL = "redis://127.0.0.1:6379/0"
_ORCH_A = "orch-peera-2026-09-08-aa11bb"
_ORCH_B = "orch-peerb-2026-09-08-cc22dd"
_HEALTH_DEADLINE = 45.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _base_env(home: Path, dsn: str, orch_id: str, trust: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ORCHESTRATORD_")
    }
    env.update(
        {
            "HOME": str(home),
            "PYTHONUNBUFFERED": "1",
            "ORCHESTRATORD_DATABASE_URL": dsn,
            "ORCHESTRATORD_INSTANCE_NAME": orch_id,
            "ORCHESTRATORD_PEER_TRUST": trust,
            "ORCHESTRATORD_REDIS_URL": _REDIS_URL,
        }
    )
    return env


def _pin_orch_id(home: Path, orch_id: str) -> None:
    path = home / ".orchestratord" / "data" / "orch_id"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(orch_id + "\n", encoding="utf-8")


async def _wait_healthy(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + _HEALTH_DEADLINE
    while time.monotonic() < deadline:
        try:
            resp = await client.get("/api/health")
            if resp.status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("daemon did not become healthy in time")


async def _wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if inspect.iscoroutine(result):
            result = await result
        if result is not None:
            return result
        await asyncio.sleep(0.2)
    raise AssertionError(f"{description} not observed within {timeout:g}s")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def cluster(tmp_path_factory):
    """Boot daemons A and B, run the §5 trust handshake both ways."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available")
    project_root = Path(__file__).resolve().parents[1]

    # Postgres gate.
    try:
        admin = await asyncpg.connect(_ADMIN_DSN)
    except OSError:
        pytest.skip("Postgres unreachable")
    try:
        for name in ("orch_peer_a", "orch_peer_b"):
            exists = await admin.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", name
            )
            if not exists:
                await admin.execute(f"CREATE DATABASE {name}")
    finally:
        await admin.close()

    # Redis gate.
    redis = None
    try:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(_REDIS_URL, decode_responses=True)
        await redis.ping()
    except Exception:  # noqa: BLE001 — any Redis failure means "unavailable"
        pytest.skip("Redis unreachable")

    tmp = tmp_path_factory.mktemp("peer_cluster")
    daemons: dict[str, SimpleNamespace] = {}
    procs: list[subprocess.Popen] = []
    clients: list[httpx.AsyncClient] = []
    log_dir = tmp / "logs"
    log_dir.mkdir()
    try:
        for name, db_name, orch_id, trust in (
            ("a", "orch_peer_a", _ORCH_A, _ORCH_B),
            ("b", "orch_peer_b", _ORCH_B, _ORCH_A),
        ):
            home = tmp / f"home-{name}"
            dsn = _DSN_TEMPLATE.format(name=db_name)
            engine = build_engine(_ALCHEMY_DSN_TEMPLATE.format(name=db_name))
            try:
                await create_schema(engine)
            finally:
                await engine.dispose()

            port = _free_port()
            _pin_orch_id(home, orch_id)
            # The daemon speaks SQLAlchemy async → dialect URL; the plain
            # DSN stays for the test's direct asyncpg assertions.
            env = _base_env(
                home, _ALCHEMY_DSN_TEMPLATE.format(name=db_name), orch_id, trust
            )
            args = [
                uv, "run", "--no-sync", "orchestratord", "serve",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--no-autopilot",
            ]
            if name == "a":
                # A only receives INVOKEs; no dispatcher needed there.
                args.append("--no-chat-daemon")
            else:
                # B's chat dispatcher must run with no resolvable backend
                # so an auto-scheduled peer turn deterministically fails.
                env["ORCHESTRATORD_CHAT_BACKEND"] = "no-such-backend"
            log = (log_dir / f"{name}.log").open("wb")

            def _spawn(
                _args=args, _env=env, _log=log
            ) -> subprocess.Popen:
                return subprocess.Popen(
                    _args,
                    cwd=project_root,
                    env=_env,
                    stdout=_log,
                    stderr=subprocess.STDOUT,
                )

            procs.append(await asyncio.to_thread(_spawn))
            client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=10.0
            )
            clients.append(client)
            await _wait_healthy(client)
            daemons[name] = SimpleNamespace(
                client=client, port=port, orch_id=orch_id, dsn=dsn, log=log
            )

        # Workspace discovery (single-user mode: no auth required).
        async def _workspace(daemon) -> str:
            resp = await daemon.client.get(
                f"/api/workspaces/by-slug/{DEFAULT_WORKSPACE_SLUG}"
            )
            assert resp.status_code == 200, resp.text
            return resp.json()["workspace_id"]

        ws_a = await _workspace(daemons["a"])
        ws_b = await _workspace(daemons["b"])

        # §5 handshake both ways, R10 trust whitelist → 200 + token.
        async def _invite(target, orch_id, workspace_id) -> str:
            resp = await target.client.post(
                "/api/peer/invite",
                json={
                    "orch_id": orch_id,
                    "name": f"peer-{orch_id}",
                    "url": f"http://127.0.0.1:{target.port}",
                    "workspace_id": workspace_id,
                    "capabilities": ["peer.invoke"],
                },
            )
            assert resp.status_code == 200, resp.text
            return resp.json()["token"]

        token_b_at_a = await _invite(daemons["a"], _ORCH_B, ws_a)
        token_a_at_b = await _invite(daemons["b"], _ORCH_A, ws_b)

        yield SimpleNamespace(
            a=daemons["a"],
            b=daemons["b"],
            ws_a=ws_a,
            ws_b=ws_b,
            token_b_at_a=token_b_at_a,
            token_a_at_b=token_a_at_b,
            redis=redis,
        )
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for client in clients:
            await client.aclose()
        for daemon in daemons.values():
            daemon.log.close()
        if redis is not None:
            await redis.aclose()
        try:
            admin = await asyncpg.connect(_ADMIN_DSN)
            try:
                for name in ("orch_peer_a", "orch_peer_b"):
                    await admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = $1 AND pid <> pg_backend_pid()",
                        name,
                    )
                    await admin.execute(f"DROP DATABASE IF EXISTS {name}")
            finally:
                await admin.close()
        except OSError:
            pass


async def _db_fetchrow(dsn: str, query: str, *args):
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


async def _db_fetch(dsn: str, query: str, *args):
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_remote_invoke_dedup_and_ordering(cluster) -> None:
    a, b = cluster.a, cluster.b

    # A-side session for B to append into.
    resp = await a.client.post(
        f"/api/workspaces/{cluster.ws_a}/chat/sessions",
        json={"prompt": "session for peer invokes"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session_id"]

    headers = {
        "Authorization": f"Bearer {cluster.token_b_at_a}",
        "X-Peer-Orchestrator-Id": b.orch_id,
    }

    async def invoke(msg_id: str, ordering: str, content: str) -> dict:
        resp = await a.client.post(
            f"/api/peer/peers/{b.orch_id}/invoke",
            headers=headers,
            json={
                "method": "POST /api/sessions/{session_id}/messages",
                "body": {"role": "user", "content": content},
                "session_id": session_id,
                "msg_id": msg_id,
                "ordering": ordering,
            },
        )
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    first = await invoke("m-1", "1", "hello from B")
    assert first["duplicate"] is False
    assert first["out_of_order"] is False

    replay = await invoke("m-1", "1", "hello from B")  # D18: same msg_id
    assert replay["duplicate"] is True
    assert replay["body"] == first["body"]

    gap = await invoke("m-2", "3", "skipped seq 2")  # D19: ordering gap
    assert gap["out_of_order"] is True

    rows = await _db_fetch(
        a.dsn,
        "SELECT role, content, author_label FROM messages "
        "WHERE session_id = $1 ORDER BY seq",
        session_id,
    )
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "session for peer invokes"),  # the §6.1c initial prompt
        ("user", "hello from B"),
        ("user", "skipped seq 2"),
    ]
    assert all(r["author_label"] == b.orch_id for r in rows[1:])

    audit = await _db_fetch(
        a.dsn,
        "SELECT payload_jsonb->>'peer_call_id' AS call, "
        "payload_jsonb->>'out_of_order' AS ooo, invited_by_orch_id "
        "FROM audit_log WHERE action = 'peer.invoke.message' AND target_id = $1 "
        "ORDER BY created_at",
        session_id,
    )
    flags = {row["call"]: row["ooo"] == "true" for row in audit}
    assert flags == {"m-1": False, "m-2": True}
    assert all(row["invited_by_orch_id"] == b.orch_id for row in audit)


@pytest.mark.asyncio(loop_scope="module")
async def test_cross_daemon_session_auto_schedules(cluster) -> None:
    b = cluster.b
    resp = await b.client.post(
        f"/api/peer/peers/{cluster.a.orch_id}/sessions",
        headers={
            "Authorization": f"Bearer {cluster.token_a_at_b}",
            "X-Peer-Orchestrator-Id": cluster.a.orch_id,
        },
        json={
            "workspace_id": cluster.ws_b,
            "prompt": "peer auto turn",
            "msg_id": "s-1",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    session_id = body["session_id"]

    # B's real claim loop takes the pending session (pending → running);
    # the unresolvable backend makes the terminal status "failed".
    _TERMINAL = {"completed", "failed", "stopped"}

    async def _status() -> str | None:
        row = await _db_fetchrow(
            b.dsn,
            "SELECT status FROM sessions WHERE id = $1",
            session_id,
        )
        status = row["status"] if row else None
        return status if status in _TERMINAL else None

    terminal = await _wait_for(_status, 20.0, "peer session terminal status")
    assert terminal == "failed"

    audit = await _db_fetchrow(
        b.dsn,
        "SELECT invited_by_orch_id, invited_by_peer_call_id FROM audit_log "
        "WHERE action = 'peer.session.create' AND target_id = $1",
        session_id,
    )
    assert audit is not None
    assert audit["invited_by_orch_id"] == cluster.a.orch_id
    assert audit["invited_by_peer_call_id"] == "s-1"


@pytest.mark.asyncio(loop_scope="module")
async def test_sse_delivers_remote_event_via_redis(cluster) -> None:
    b = cluster.b
    topic = "peer.agent.1.events"
    payload = {"session_id": "sse-check", "text": "cross-process hello"}
    headers = {
        "Authorization": f"Bearer {cluster.token_a_at_b}",
        "X-Peer-Orchestrator-Id": cluster.a.orch_id,
    }

    received: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    async def read_stream() -> None:
        async with b.client.stream(
            "GET",
            f"/api/peer/peers/{cluster.a.orch_id}/events",
            params={"topics": topic},
            headers=headers,
        ) as resp:
            assert resp.status_code == 200, resp.text
            deadline = time.monotonic() + 20.0
            async for line in resp.aiter_lines():
                if time.monotonic() > deadline:
                    return
                if not line.startswith("data: "):
                    continue  # keep-alives arrive as ": ..." comments
                frame = json.loads(line[len("data: ") :])
                if frame.get("topic") == topic and not received.done():
                    received.set_result(frame)
                    return

    reader = asyncio.create_task(read_stream())
    try:
        # A's D22 channel: B's relay ingests (origin ≠ B); A's relay
        # echo-guards it away. Retry-publish covers relay startup races.
        channel = peer_channel(cluster.a.orch_id, topic)
        frame = json.dumps({"topic": topic, "payload": payload})
        deadline = time.monotonic() + 15.0
        while not received.done() and time.monotonic() < deadline:
            await cluster.redis.publish(channel, frame)
            try:
                await asyncio.wait_for(asyncio.shield(received), timeout=0.5)
            except TimeoutError:
                pass
        assert received.done(), "SSE stream never delivered the relayed frame"
        assert received.result()["payload"] == payload
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
