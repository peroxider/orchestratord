"""NG8 自动轮换收尾 — PeerClient token self-rotation unit tests.

Covers the client side of ``POST /api/peer/self/rotate-token``:

* :meth:`PeerClient.rotate_token` swaps the in-memory token and sends
  the *old* token as the bearer on the rotation call itself.
* Transport-only clients (no ``base_url``) raise instead of silently
  doing nothing.
* A failed rotation leaves the current token untouched.
* :meth:`PeerClient.start_auto_token_rotation` gates on the
  ``ORCHESTRATORD_PEER_TOKEN_ROTATE_SECONDS`` env var, is idempotent,
  and its task is cancelled by :meth:`PeerClient.close`.
* The ``peer rotate`` CLI subcommand exists with the expected flags.

Server-side behaviour (grace window, NG8 fallback in
``require_peer_auth``) is covered in ``tests/api/test_peer_federation_api.py``.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx
import pytest

from orchestratord.cli.peer import add_peer_parser
from orchestratord.peer.client import PeerClient, PeerClientError
from orchestratord.peer.nonce_store import NonceStore


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://remote.test")
            response = httpx.Response(
                self.status_code, request=request
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttp:
    """Records POST calls; replays a queued response per call."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def post(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls.append((url, dict(headers or {})))
        if not self._responses:
            raise AssertionError("unexpected extra POST")
        return self._responses.pop(0)


def _make_client(http: Any, token: str = "tok-old") -> PeerClient:
    return PeerClient(
        orch_id="orch-A1",
        token=token,
        transport_factory=_async_noop_factory,
        nonce_store=NonceStore("/tmp/ng8-test-nonces-client.db"),
        base_url="http://remote.test",
        http=http,
    )


async def _async_noop_factory() -> Any:  # pragma: no cover - never awaited
    raise AssertionError("transport factory must not be used by rotate_token")


@pytest.mark.asyncio
async def test_rotate_token_swaps_token_and_carries_old_auth() -> None:
    http = _FakeHttp([_FakeResponse({"status": "rotated", "token": "tok-new"})])
    client = _make_client(http, token="tok-old")

    new_token = await client.rotate_token()

    assert new_token == "tok-new"
    assert client._token == "tok-new"
    url, headers = http.calls[0]
    assert url.endswith("/api/peer/self/rotate-token")
    # The rotation call itself must authenticate with the *old* token —
    # that is the whole point of the grace window.
    assert headers["Authorization"] == "Bearer tok-old"
    assert headers["X-Peer-Orchestrator-Id"] == "orch-A1"


@pytest.mark.asyncio
async def test_rotate_token_without_base_url_raises() -> None:
    client = PeerClient(
        orch_id="orch-A1",
        token="tok-old",
        transport_factory=_async_noop_factory,
        nonce_store=NonceStore("/tmp/ng8-test-nonces-nourl.db"),
    )
    with pytest.raises(PeerClientError, match="base_url"):
        await client.rotate_token()


@pytest.mark.asyncio
async def test_rotate_token_bad_response_raises_and_keeps_old_token() -> None:
    http = _FakeHttp([_FakeResponse({}, status_code=500)])
    client = _make_client(http, token="tok-old")

    with pytest.raises(httpx.HTTPStatusError):
        await client.rotate_token()

    assert client._token == "tok-old"


@pytest.mark.asyncio
async def test_auto_rotation_gating_and_close_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCHESTRATORD_PEER_TOKEN_ROTATE_SECONDS", raising=False)
    client = _make_client(_FakeHttp([]))

    # Unset env → disabled.
    assert client.start_auto_token_rotation() is False
    assert client._rotate_task is None
    # Explicit 0 → disabled.
    assert client.start_auto_token_rotation(0) is False
    # Explicit interval → started, and idempotent.
    assert client.start_auto_token_rotation(3600) is True
    task = client._rotate_task
    assert task is not None
    assert client.start_auto_token_rotation(3600) is True
    assert client._rotate_task is task

    await client.close()
    assert client._rotate_task is None
    await asyncio.sleep(0)
    assert task.done()


def test_peer_cli_parser_has_rotate() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="top", required=True)
    add_peer_parser(subparsers)

    args = parser.parse_args(
        ["peer", "rotate", "--orch-id", "orch-A1",
         "--workspace-id", "00000000-0000-0000-0000-000000000001"]
    )
    assert args.peer_subcommand == "rotate"
    assert args.orch_id == "orch-A1"
    assert args.workspace_id == "00000000-0000-0000-0000-000000000001"
    # Default: None → resolved from PeerConfig at runtime.
    assert args.grace_seconds is None

    explicit = parser.parse_args(
        ["peer", "rotate", "--orch-id", "orch-A1",
         "--workspace-id", "00000000-0000-0000-0000-000000000001",
         "--grace-seconds", "0"]
    )
    assert explicit.grace_seconds == 0.0
