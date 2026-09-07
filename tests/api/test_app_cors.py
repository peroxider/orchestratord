"""CORS contract for the Next.js console (apps/web) calling this API.

The console is served on its own origin (:3100 by default) and calls the
REST API cross-origin; the browser blocks every response unless the API
answers preflights and echoes the origin.  WebSocket (``/ws``) is exempt
from browser CORS enforcement.  These tests use ``OPTIONS`` preflights
only, so they never touch the database.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestratord.api.app import create_app

_WEB_ORIGIN = "http://localhost:3100"


def _preflight(client: TestClient, origin: str) -> object:
    return client.options(
        "/api/skills",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


def test_default_web_origin_allowed() -> None:
    client = TestClient(create_app())
    response = _preflight(client, _WEB_ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _WEB_ORIGIN


def test_cors_env_override_replaces_defaults(monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATORD_CORS_ORIGINS", "https://console.example.com")
    client = TestClient(create_app())
    response = _preflight(client, "https://console.example.com")
    assert response.status_code == 200
    assert (
        response.headers["access-control-allow-origin"]
        == "https://console.example.com"
    )
    # The override replaces (not extends) the built-in origins.
    default_response = _preflight(client, _WEB_ORIGIN)
    assert "access-control-allow-origin" not in default_response.headers


def test_unknown_origin_gets_no_header() -> None:
    client = TestClient(create_app())
    response = _preflight(client, "http://evil.example.com")
    assert "access-control-allow-origin" not in response.headers
