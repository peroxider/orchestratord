"""Agent Card + persistent orch_id coverage (DESIGN §4.3, AC4).

The orch_id must be generated once and survive daemon restarts via
``~/.orchestratord/data/orch_id`` (tests pin ``ORCH_ID_PATH`` to
``tmp_path`` so the real home directory is never touched), and the
card must advertise ``peer/1`` — borrowed A2A field naming without
claiming A2A compatibility (ADR-001 D1/D20).
"""

from __future__ import annotations

import json
import re

from orchestratord._version import __version__
from orchestratord.peer.card import (
    DEFAULT_CAPABILITIES,
    build_agent_card,
    ensure_orch_id,
    load_orch_id,
)

# -- orch_id persistence (AC4) --

def test_orch_id_generated_and_persisted(tmp_path) -> None:
    path = tmp_path / "orch_id"
    first = ensure_orch_id(path=path)
    assert first == load_orch_id(path)
    # A second call — e.g. after a daemon restart — must reuse it.
    assert ensure_orch_id(path=path) == first


def test_orch_id_format_matches_spec_example(tmp_path) -> None:
    # §4.3: orch-{instance-slug}-{YYYY-MM-DD}-{6 hex}
    orch_id = ensure_orch_id(instance_name="A1 Server", path=tmp_path / "orch_id")
    assert re.fullmatch(
        r"orch-a1-server-\d{4}-\d{2}-\d{2}-[0-9a-f]{6}", orch_id
    )


def test_orch_id_regenerated_when_file_empty(tmp_path) -> None:
    path = tmp_path / "orch_id"
    path.write_text("  \n", encoding="utf-8")
    fresh = ensure_orch_id(path=path)
    assert fresh
    assert load_orch_id(path) == fresh


def test_orch_id_uses_env_instance_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATORD_INSTANCE_NAME", "B2")
    assert ensure_orch_id(path=tmp_path / "orch_id").startswith("orch-b2-")


def test_orch_id_slug_fallback_for_symbol_only_name(tmp_path) -> None:
    orch_id = ensure_orch_id(instance_name="///", path=tmp_path / "orch_id")
    assert orch_id.startswith("orch-orchestratord-")


def test_load_orch_id_missing_file_returns_none(tmp_path) -> None:
    assert load_orch_id(tmp_path / "nope") is None


# -- Agent Card (§4.3) --

def test_card_core_fields() -> None:
    card = build_agent_card(orch_id="orch-x", url="http://h:9001")
    assert card["protocol_version"] == "peer/1"
    assert card["orch_id"] == "orch-x"
    assert card["url"] == "http://h:9001"
    assert card["version"] == __version__
    # D1/D20: borrowed naming disclosed, A2A compatibility never claimed.
    assert card["agent_card_version"] == "draft-2026-04-a2a-style"
    assert card["inspired_by"] == ["a2a-protocol-v1.0", "mcp-2026-07"]


def test_card_default_capabilities_skills_and_schemes() -> None:
    card = build_agent_card(orch_id="o", url="u")
    assert card["capabilities"] == DEFAULT_CAPABILITIES
    assert card["defaultInputModes"] == ["text/plain", "application/json"]
    skill_ids = {s["id"] for s in card["skills"]}
    assert {"sessions.message.post", "realtime.subscribe"} <= skill_ids
    # ADR D3: the sample doc typed hmac "mutual-tls"; the card must
    # name the actual scheme.
    assert set(card["security_schemes"]) == {"bearer", "hmac"}
    assert card["security_schemes"]["hmac"]["type"] == "hmac-sha256"


def test_card_overrides() -> None:
    card = build_agent_card(
        orch_id="o",
        url="u",
        name="A1",
        description="primary daemon",
        capabilities=["peer.invoke"],
        skills=[{"id": "x"}],
        provider={"organization": "org", "contact": "c@example.com"},
    )
    assert card["name"] == "A1"
    assert card["description"] == "primary daemon"
    assert card["capabilities"] == ["peer.invoke"]
    assert card["skills"] == [{"id": "x"}]
    assert card["provider"]["organization"] == "org"


def test_card_is_json_serializable() -> None:
    # The card is served as-is by the well-known route.
    json.dumps(build_agent_card(orch_id="o", url="u"))
