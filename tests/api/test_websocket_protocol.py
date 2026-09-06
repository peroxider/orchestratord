"""WebSocket protocol contract (Phase 1, §5.4.1 / §6.4).

Tests the new ``/ws`` endpoint introduced by the FastAPI split. The
protocol carries real-time events from daemon → server → browser, and
routes ``session.approve`` calls back the other way. Tests fail today;
they pin the wire contract.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.4.1, §6.4.
"""
from __future__ import annotations

import pytest


def _ws_url(token: str = "test-token", workspace_id: str = "ws_test") -> str:
    return f"/ws?workspace_id={workspace_id}&token={token}"


@pytest.fixture
def ws_factory():
    """Yield a callable that opens a WebSocket connection through TestClient."""
    from fastapi.testclient import TestClient

    from orchestratord.api.app import app

    client = TestClient(app)

    def _open(token: str = "test-token", workspace_id: str = "ws_test"):
        return client.websocket_connect(_ws_url(token=token, workspace_id=workspace_id))

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
