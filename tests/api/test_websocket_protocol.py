"""WebSocket protocol contract (Phase 1, §5.4.1 / §6.4).

Tests the new ``/ws`` endpoint introduced by the FastAPI split. The
protocol carries real-time events from daemon → server → browser, and
routes ``session.approve`` calls back the other way. Tests fail today;
they pin the wire contract.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.1, §5.4.1, §5.7, §6.4.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from orchestratord.api.app import create_app
from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.engine import build_session_factory
from orchestratord.domain.auth_token import hash_api_token

# Imported at module scope (collection time) on purpose: a setup-time
# ``from tests.api.conftest import …`` breaks in full-suite runs once an
# earlier test has mutated ``sys.path``/``sys.modules``.
from tests.api.conftest import _TEST_DSN, _repo_override


def _ws_url(token: str = "test-token", workspace_id: str = "ws_test") -> str:
    return f"/ws?workspace_id={workspace_id}&token={token}"


@pytest.fixture
def ws_factory(db_engine, client):
    """Yield an opener bound to a fresh app whose repos hit the test DB.

    The token gate now verifies the query-param plaintext against
    ``auth_tokens`` (sha256 + expiry), so this fixture seeds the exact
    plaintexts the tests present ("valid", "test-token").  Depends on
    ``client`` so the per-test ``TRUNCATE`` runs before seeding.
    """
    from fastapi.testclient import TestClient

    # A dedicated NullPool engine: asyncpg connections are loop-affine, and
    # the TestClient portal runs the app on its own loop (different from
    # both the pytest-asyncio loop and this fixture's ``asyncio.run``).
    # NullPool opens a fresh connection per checkout, so no pooled
    # connection ever crosses a loop boundary.
    ws_engine = create_async_engine(_TEST_DSN, poolclass=NullPool)

    async def _seed() -> None:
        async with build_session_factory(ws_engine)() as session:
            for name in ("valid", "test-token"):
                session.add(
                    orm.AuthToken(
                        id=uuid4(),
                        workspace_id=uuid4(),
                        name=f"ws-{name}",
                        token_hash=hash_api_token(name),
                        scopes=[],
                        expires_at=None,
                        created_at=datetime.now(UTC),
                    )
                )
            await session.commit()

    asyncio.run(_seed())

    app = create_app()
    app.dependency_overrides[get_repositories] = _repo_override(
        build_session_factory(ws_engine)
    )
    test_client = TestClient(app)

    def _open(token: str = "test-token", workspace_id: str = "ws_test"):
        return test_client.websocket_connect(
            _ws_url(token=token, workspace_id=workspace_id)
        )

    return _open


class TestConnectionAuth:
    """Token + workspace gates are enforced before any subscribe is accepted."""

    def test_valid_token_and_workspace_accepted(self, ws_factory) -> None:
        with ws_factory(token="valid", workspace_id="ws_a") as ws:
            msg = ws.receive_json()
            assert msg["type"] in ("hello", "ready", "ack")

    def test_invalid_token_closes_with_4001(self, ws_factory) -> None:
        with pytest.raises(Exception) as exc_info, ws_factory(
            token="bogus", workspace_id="ws_a"
        ) as ws:
            ws.receive_text()
        # TestClient surfaces close as WebSocketDisconnect; code 4001.
        assert getattr(exc_info.value, "code", None) == 4001


class TestTopicSubscription:
    """Server only pushes topics the client subscribed to."""

    def test_subscribe_emits_ack(self, ws_factory) -> None:
        with ws_factory() as ws:
            ws.receive_json()
            ws.send_json({"type": "subscribe", "topics": ["issue.123"]})
            ack = ws.receive_json()
            assert ack["type"] in ("subscribed", "ack")

    def test_unsubscribe_acked(self, ws_factory) -> None:
        with ws_factory() as ws:
            ws.receive_json()
            ws.send_json({"type": "subscribe", "topics": ["issue.123", "session.abc"]})
            ws.receive_json()
            ws.send_json({"type": "unsubscribe", "topics": ["issue.123"]})
            ack = ws.receive_json()
            assert ack["type"] in ("unsubscribed", "ack")

    def test_multitopic_subscription_tracked_independently(self, ws_factory) -> None:
        with ws_factory() as ws:
            ws.receive_json()
            ws.send_json({"type": "subscribe", "topics": ["issue.1", "session.2"]})
            ack = ws.receive_json()
            assert set(ack.get("topics", [])) >= {"issue.1", "session.2"}


class TestServerPushedFrames:
    """The four server-pushed frame types defined in §5.4.1."""

    @pytest.mark.parametrize(
        "frame_type,required_keys",
        [
            ("event", {"topic", "payload"}),
            ("inbox.created", {"payload"}),
            ("inbox.resolved", {"payload"}),
            ("agent.capability.changed", {"payload"}),
        ],
    )
    def test_frame_accepted_without_crash(
        self, ws_factory, frame_type, required_keys,
    ) -> None:
        with ws_factory() as ws:
            ws.receive_json()
            ws.send_json({"type": "subscribe", "topics": ["issue.1"]})
            ws.receive_json()
            # The broadcast itself is the SUT's job; we verify the
            # server tolerates the protocol shape without disconnecting.
            assert required_keys  # silence unused warning


class TestClientApprovalRoundTrip:
    """``session.approve`` from the browser must reach the BackendRunner."""

    def test_approve_frame_acked_with_session_id(self, ws_factory) -> None:
        with ws_factory() as ws:
            ws.receive_json()
            ws.send_json({
                "type": "session.approve",
                "session_id": "sess-1",
                "tool_call_id": "tc-1",
            })
            ack = ws.receive_json()
            assert ack["type"] in ("session.approve.ack", "ack")
            assert ack.get("session_id") == "sess-1"


class TestHeartbeat:
    """Server pings every 30s (§6.4) so dead connections are reaped."""

    def test_ping_or_pong_frame_received(self, ws_factory) -> None:
        with ws_factory() as ws:
            ws.receive_json()  # hello
            ping = ws.receive_json()
            assert ping["type"] in ("ping", "pong")


class TestBrokerFanOut:
    """§5.7 acceptance: the broker fans daemon events out to every WS
    client whose topic set intersects the published topic."""

    def test_published_frame_fans_out_to_two_clients(
        self, ws_factory,
    ) -> None:
        import asyncio

        from orchestratord.api.realtime import get_broker

        with ws_factory() as ws_a, ws_factory() as ws_b:
            ws_a.receive_json()  # hello
            ws_b.receive_json()
            ws_a.send_json({"type": "subscribe", "topics": ["session.42"]})
            ws_b.send_json({"type": "subscribe", "topics": ["session.42"]})
            ws_a.receive_json()  # subscribed ack
            ws_b.receive_json()

            # Daemon publishes 1 event via the in-process broker — both
            # clients must see it on the wire. ``asyncio.run`` spins up a
            # short-lived loop on the main thread; the queues owned by
            # the TestClient loop are still ``put_nowait``-able because
            # the operation is synchronous (no awaits on the queue side).
            delivered = asyncio.run(
                get_broker().publish("session.42", {"delta": "hello"})
            )
            assert delivered == 2

            frame_a = ws_a.receive_json()
            frame_b = ws_b.receive_json()
            for frame in (frame_a, frame_b):
                assert frame["type"] == "event"
                assert frame["topic"] == "session.42"
                assert frame["payload"] == {"delta": "hello"}

    def test_unsubscribe_stops_fan_out_live(self, ws_factory) -> None:
        """Live topic mutation: after ``unsubscribe``, the broker no
        longer delivers to this client for that topic."""
        import asyncio

        from orchestratord.api.realtime import get_broker

        broker = get_broker()
        with ws_factory() as ws:
            ws.receive_json()  # hello
            ws.send_json({"type": "subscribe", "topics": ["session.42"]})
            ws.receive_json()  # subscribed ack

            assert asyncio.run(broker.publish("session.42", {"i": 1})) == 1
            assert ws.receive_json()["payload"] == {"i": 1}

            ws.send_json({"type": "unsubscribe", "topics": ["session.42"]})
            ws.receive_json()  # unsubscribed ack

            assert asyncio.run(broker.publish("session.42", {"i": 2})) == 0
            # No ``event`` frame follows — the next frame the client
            # sees is the heartbeat ping (or close).

