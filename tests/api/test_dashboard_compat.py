"""Phase 0 FastAPI split: dashboard compat shim contract.

These tests pin the public surface that ``cli/dashboard.py``
``DashboardHandler`` previously served. The new FastAPI app in
``apps/api/main.py`` MUST preserve every one of these routes and verbs —
``orchestratord dashboard`` and the chat UI must keep working during the
split. Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.5.1, §10.1.

Tests fail today (the new app does not exist yet); they encode the
contract the implementation must satisfy.
"""
from __future__ import annotations

import pytest

from tests.api.conftest import _repo_override


def _client():
    """A fresh app with the token gate lifted (route-shape contract only).

    These tests pin the legacy ``DashboardHandler`` surface — routes, verbs,
    payload shapes — not authentication; the gate itself is exercised in
    ``tests/api/test_auth.py``.  Repos are overridden to the dedicated test
    database (a fresh ``create_app()`` would otherwise fall through to the
    real ``get_repositories`` and its default DSN — the shared ``multica``
    instance).  ``NullPool`` keeps connections from crossing the TestClient
    portal loop and the pytest-asyncio loop.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from fastapi.testclient import TestClient

    from orchestratord.api.app import create_app
    from orchestratord.api.db import get_repositories
    from orchestratord.api.deps import require_auth
    from orchestratord.db.engine import build_session_factory
    from tests.api.conftest import _TEST_DSN

    ws_engine = create_async_engine(_TEST_DSN, poolclass=NullPool)
    application = create_app()
    application.dependency_overrides[get_repositories] = _repo_override(
        build_session_factory(ws_engine)
    )
    application.dependency_overrides[require_auth] = lambda: None
    return TestClient(application)


class TestLiveViewHtmlSurface:
    """The HTML pages the legacy ``DashboardHandler`` served must still render."""

    def test_root_returns_dashboard_or_redirects(self) -> None:
        client = _client()
        resp = client.get("/")
        assert resp.status_code in (200, 307, 308)

    def test_dashboard_path_returns_html(self) -> None:
        client = _client()
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        body = resp.text
        assert "Orchestratord" in body or "LiveView" in body

    def test_chat_path_returns_html(self) -> None:
        client = _client()
        resp = client.get("/chat")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestLegacyReadApi:
    """JSON read endpoints that ``DashboardHandler.do_GET`` previously served."""

    def test_state_endpoint_returns_aggregated_payload(self) -> None:
        client = _client()
        resp = client.get("/api/state")
        assert resp.status_code == 200
        payload = resp.json()
        for key in ("active_sessions", "total_turns", "total_tools"):
            assert key in payload, f"missing key {key!r} in /api/state"

    def test_runs_list_endpoint(self) -> None:
        client = _client()
        resp = client.get("/api/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, (list, dict))


class TestLegacyControlApi:
    """Mutation routes the chat UI relies on must keep their URL + verb."""

    @pytest.mark.parametrize(
        "verb,path",
        [
            ("post", "/api/runs/{rid}/pause"),
            ("post", "/api/runs/{rid}/resume"),
            ("post", "/api/runs/{rid}/stop"),
            ("post", "/api/runs/{rid}/messages"),
            ("post", "/api/runs/{rid}/approve"),
            ("post", "/api/runs/{rid}/deny"),
        ],
    )
    def test_control_route_exists(self, verb: str, path: str) -> None:
        client = _client()
        method = getattr(client, verb)
        resp = method(path.format(rid="__no_such_run__"))
        # 404 (resource) / 422 (validation) are acceptable;
        # 405 would mean the route was dropped during the FastAPI split.
        assert resp.status_code != 405, (
            f"{verb.upper()} {path} returned 405 — route removed in FastAPI split"
        )


class TestLegacySseStream:
    """The SSE endpoint for live events must remain on the same path."""

    def test_runs_events_stream_returns_event_stream(self) -> None:
        client = _client()
        with client.stream("GET", "/api/runs/__no_such_run__/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            # Read at least one byte to confirm the stream is open.
            next(iter(resp.iter_bytes()), b"")


class TestCompatShimRouting:
    """The compat shim must NOT swallow requests intended for the new API."""

    def test_new_v1_prefix_does_not_collide_with_legacy(self) -> None:
        client = _client()
        # OpenAPI auto-gen lives at /api/v1/openapi.json; an explicit
        # 404 on a non-existent /api/v1/anything confirms the prefix
        # is free for the new routers to claim.
        resp = client.get("/api/v1/__no_such_route__")
        assert resp.status_code == 404
