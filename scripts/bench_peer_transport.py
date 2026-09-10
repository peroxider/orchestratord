"""PR-B7 transport evaluation spike — REST invoke vs frame INVOKE.

Stands up ONE daemon in-process (SQLite + uvicorn on a real port, the
same harness as ``tests/peer_integration/test_two_daemon_frame.py``)
and benchmarks the two peer transports end-to-end over real sockets:

* **REST** — one ``POST /api/peer/peers/{orch}/invoke`` per call
  (request-line auth + dedup cache + DB query each time),
* **Frame** — the PR-B9 batch-POST JSONL binding of
  ``POST /peer/v1/stream`` (one HTTP request per logical batch);
  INVOKE → RESULT sequential round-trips, then concurrent
  (pipelined) batches,
* **Compression** — each scenario repeated with
  ``ORCHESTRATORD_PEER_FRAME_COMPRESS_BYTES`` on (default 4096) vs 0.

Output: a markdown table on stdout (also written to
``bench_peer_transport_results.md`` next to this script when
``--save`` is passed). This is a spike artifact — numbers describe the
dev machine, not production.

Usage::

    uv run python scripts/bench_peer_transport.py [--n-small 300]
        [--n-large 100] [--batch 50 --rounds 4] [--save]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import statistics
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx

from orchestratord.api.app import create_app
from orchestratord.api.deps import TokenBucket, reset_peer_rate_bucket
from orchestratord.api.routers.peer import reset_dispatcher
from orchestratord.db import models as orm
from orchestratord.db.engine import build_engine, create_schema
from orchestratord.domain.auth_token import hash_api_token, issue_api_token
from orchestratord.peer.client import PeerClient
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.registry import set_peer_status, upsert_peer

METHOD = "GET /api/workspaces/{workspace_id}/sessions"
_PAD_KB = 64


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_ADMIN_DSN = "postgresql://multica:multica@127.0.0.1:5432/multica"
_BENCH_DB = "orchestratord_bench"
_BENCH_DSN = f"postgresql+asyncpg://multica:multica@127.0.0.1:5432/{_BENCH_DB}"


async def _setup() -> tuple[object, str, uuid.UUID]:
    """Seed a scratch Postgres DB: accepted peer + token.

    Mirrors ``tests/db_integration/conftest.py`` — the §6.1 schema is
    PostgreSQL-flavoured (JSONB), so SQLite is not viable here. The
    ``orchestratord_bench`` database is dropped and recreated each run.
    """
    import asyncpg

    try:
        admin = await asyncpg.connect(_ADMIN_DSN, timeout=3)
    except Exception as exc:
        raise SystemExit(
            "Postgres unavailable at 127.0.0.1:5432 — bench needs a live "
            f"instance (same as the db_integration suite): {exc}"
        )
    try:
        await admin.execute(
            f"DROP DATABASE IF EXISTS {_BENCH_DB} WITH (FORCE)"
        )
        await admin.execute(f"CREATE DATABASE {_BENCH_DB}")
    finally:
        await admin.close()

    os.environ["ORCHESTRATORD_DATABASE_URL"] = _BENCH_DSN
    os.environ["ORCHESTRATORD_PEER_NONCE_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="peer-bench-"), "nonces.db"
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = build_engine()
    await create_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws_id = uuid.uuid4()
    async with factory() as session:
        peer = await upsert_peer(
            session,
            workspace_id=ws_id,
            orch_id="orch-BENCH",
            name="Bench",
            url="http://r:9001",
            capabilities=["peer.invoke"],
            client_kind="v2",
        )
        plaintext, token_hash = issue_api_token()
        token_row = orm.AuthToken(
            id=uuid.uuid4(),
            workspace_id=ws_id,
            name=f"peer:{peer.orch_id}",
            token_hash=hash_api_token(plaintext),
            scopes=["peer.invoke"],
            expires_at=None,
            created_at=datetime.now(UTC),
        )
        session.add(token_row)
        await session.flush()
        peer.token_id = token_row.id
        await set_peer_status(session, peer, "accepted")
        await session.commit()
        await session.refresh(peer)
    await engine.dispose()
    return peer, plaintext, ws_id


def _serve_in_thread(app, port: int):
    import uvicorn

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    return server, thread


def _stats(latencies_ms: list[float]) -> dict[str, float]:
    ordered = sorted(latencies_ms)
    n = len(ordered)
    return {
        "n": n,
        "p50": ordered[n // 2],
        "p95": ordered[min(n - 1, int(n * 0.95))],
        "mean": statistics.fmean(ordered),
        "rps": 1000.0 / statistics.fmean(ordered),
    }


def _padded_body(ws_id: uuid.UUID, pad: bool) -> dict:
    body: dict = {"workspace_id": str(ws_id)}
    if pad:
        body["pad"] = "x" * (_PAD_KB * 1024)
    return body


def bench_rest(
    base_url: str,
    token: str,
    orch_id: str,
    ws_id: uuid.UUID,
    *,
    pad: bool,
    n: int,
) -> dict[str, float]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Peer-Orchestrator-Id": orch_id,
    }
    latencies: list[float] = []
    with httpx.Client(timeout=30) as http:
        for i in range(n):
            payload = {
                "method": METHOD,
                "body": _padded_body(ws_id, pad),
                "msg_id": f"rest-{pad}-{i}",
            }
            start = time.perf_counter()
            resp = http.post(
                f"{base_url}/api/peer/peers/{orch_id}/invoke",
                json=payload,
                headers=headers,
            )
            elapsed = (time.perf_counter() - start) * 1000
            resp.raise_for_status()
            latencies.append(elapsed)
    return _stats(latencies)


async def _bench_frame_client(
    client: PeerClient, ws_id: uuid.UUID, *, pad: bool, n: int
) -> dict[str, float]:
    latencies: list[float] = []
    for i in range(n):
        start = time.perf_counter()
        result = await client.invoke(METHOD, _padded_body(ws_id, pad))
        elapsed = (time.perf_counter() - start) * 1000
        if result.status != 200:
            raise RuntimeError(f"frame invoke failed: {result.status}")
        latencies.append(elapsed)
    return _stats(latencies)


async def _bench_frame_pipelined(
    client: PeerClient, ws_id: uuid.UUID, *, batch: int, rounds: int
) -> dict[str, float]:
    """Concurrent INVOKE batches through one stream (pipelining)."""
    wall: list[float] = []
    for _ in range(rounds):
        start = time.perf_counter()
        results = await asyncio.gather(
            *(
                client.invoke(METHOD, {"workspace_id": str(ws_id)})
                for _ in range(batch)
            )
        )
        elapsed = (time.perf_counter() - start) * 1000
        bad = [r.status for r in results if r.status != 200]
        if bad:
            raise RuntimeError(f"pipelined invoke failures: {bad[:3]}")
        wall.append(elapsed)
    stats = _stats([w / batch for w in wall])
    # Throughput by wall clock: batch calls complete per batch wall-time.
    stats["rps"] = batch * 1000.0 / statistics.fmean(wall)
    return stats


async def _run_frame(
    base_url: str, token: str, orch_id: str, ws_id: uuid.UUID,
    *, n_small: int, n_large: int, batch: int, rounds: int,
) -> dict[str, dict[str, float]]:
    from orchestratord.peer.transports.https_frame import (
        HttpsFrameTransport,
    )

    client = PeerClient(
        orch_id=orch_id,
        token=token,
        transport_factory=lambda: HttpsFrameTransport.connect(
            url=f"{base_url}/peer/v1/stream",
            orch_id=orch_id,
            token=token,
        ),
        nonce_store=NonceStore(
            os.environ["ORCHESTRATORD_PEER_NONCE_PATH"]
        ),
        base_url=base_url,
        transport="frame",
        frame_url=f"{base_url}/peer/v1/stream",
    )
    await client.open()
    try:
        return {
            "frame_small": await _bench_frame_client(
                client, ws_id, pad=False, n=n_small
            ),
            "frame_large": await _bench_frame_client(
                client, ws_id, pad=True, n=n_large
            ),
            "frame_pipelined": await _bench_frame_pipelined(
                client, ws_id, batch=batch, rounds=rounds
            ),
        }
    finally:
        await client.close()


def _fmt(stats: dict[str, float]) -> str:
    return (
        f"p50={stats['p50']:.1f}ms p95={stats['p95']:.1f}ms "
        f"mean={stats['mean']:.1f}ms ≈{stats['rps']:.0f} rps"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-small", type=int, default=300)
    parser.add_argument("--n-large", type=int, default=100)
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    peer, plaintext, ws_id = asyncio.run(_setup())

    # D25 must not throttle the bench.
    reset_peer_rate_bucket()
    import orchestratord.api.deps as deps

    deps._PEER_RATE_BUCKET = deps.TokenBucket(1_000_000.0, 1_000_000)

    port = _free_port()
    app = create_app()
    _serve_in_thread(app, port)
    base_url = f"http://127.0.0.1:{port}"
    print(f"daemon on {base_url}  db={_BENCH_DB}")

    rows: list[tuple[str, str, str]] = []
    for compress in (False, True):
        os.environ["ORCHESTRATORD_PEER_FRAME_COMPRESS_BYTES"] = (
            "4096" if compress else "0"
        )
        label = f"compress={'on' if compress else 'off'}"
        frame: dict[str, dict[str, float]] | None = None
        frame_verdict = ""
        try:
            frame = asyncio.run(
                _run_frame(
                    base_url, plaintext, peer.orch_id, ws_id,
                    n_small=args.n_small, n_large=args.n_large,
                    batch=args.batch, rounds=args.rounds,
                )
            )
        except Exception as exc:
            frame_verdict = f"FAILED — {type(exc).__name__}: {exc}"
        rows.append((f"REST small ({label})",
                     "1 req / conn",
                     _fmt(bench_rest(
                         base_url, plaintext, peer.orch_id, ws_id,
                         pad=False, n=args.n_small,
                     ))))
        rows.append((f"REST large 64KB ({label})",
                     "1 req / conn",
                     _fmt(bench_rest(
                         base_url, plaintext, peer.orch_id, ws_id,
                         pad=True, n=args.n_large,
                     ))))
        if frame is not None:
            rows.append((f"Frame small ({label})", "seq round-trip",
                         _fmt(frame["frame_small"])))
            rows.append((f"Frame large 64KB ({label})", "seq round-trip",
                         _fmt(frame["frame_large"])))
            rows.append((
                f"Frame pipelined ×{args.batch} ({label})",
                "concurrent batch",
                _fmt(frame["frame_pipelined"]),
            ))
        else:
            rows.append((f"Frame all scenarios ({label})",
                         "batch-POST session", frame_verdict))

    lines = [
        "# PR-B7 transport bench results",
        "",
        f"date: {datetime.now(UTC).isoformat(timespec='seconds')}  ",
        f"n_small={args.n_small} n_large={args.n_large} "
        f"batch={args.batch}×{args.rounds}  ",
        f"payload: small=~120B large=~{_PAD_KB}KB padded "
        "(read-only sessions method both sides)",
        "",
        "| scenario | pattern | result |",
        "|---|---|---|",
    ]
    lines += [f"| {name} | {pat} | {res} |" for name, pat, res in rows]
    out = "\n".join(lines) + "\n"
    print()
    print(out)
    if args.save:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "bench_peer_transport_results.md",
        )
        with open(path, "w") as fh:
            fh.write(out)
        print(f"saved → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
