"""/.well-known/agent.json contract (DESIGN §4.3, PR2).

Discovery is intentionally public — §5 has it precede any shared
token, and the card carries no workspace data — so these tests build a
fresh app with *no* auth override and expect 200 without a bearer
token. ``card.ORCH_ID_PATH`` is patched to ``tmp_path`` so tests never
write to the real ``~/.orchestratord``.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orchestratord.api.app import create_app
from orchestratord.peer import card


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(card, "ORCH_ID_PATH", tmp_path / "data" / "orch_id")
    monkeypatch.setenv("ORCHESTRATORD_PEER_PUBLIC_URL", "https://a1.example.com")
    monkeypatch.delenv("ORCHESTRATORD_INSTANCE_NAME", raising=False)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_agent_card_is_public(client) -> None:
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol_version"] == "peer/1"
    assert body["url"] == "https://a1.example.com"
    assert body["orch_id"].startswith("orch-")


async def test_agent_card_orch_id_stable_across_requests(client, tmp_path) -> None:
    r1 = (await client.get("/.well-known/agent.json")).json()
    r2 = (await client.get("/.well-known/agent.json")).json()
    assert r1["orch_id"] == r2["orch_id"]
    assert (tmp_path / "data" / "orch_id").exists()


async def test_agent_card_url_defaults_to_local(client, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATORD_PEER_PUBLIC_URL", raising=False)
    body = (await client.get("/.well-known/agent.json")).json()
    assert body["url"].startswith("http://127.0.0.1:")
