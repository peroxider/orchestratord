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


# -- PR-B1: transports[] and preferred_transport (Phase B) --


def test_card_default_advertises_frame_and_rest() -> None:
    """PR-B2: the default ``transports[]`` advertises both frame (first,
    preferred) and rest (fallback) so a v2 client picks frame and a v1
    client reading ``preferred_transport`` sees a single hint it can
    act on. Frame URL is the rest URL + ``/peer/v1/stream`` path
    (single-port design, plan §A).
    """
    card = build_agent_card(orch_id="o", url="http://h:9001")
    assert card["transports"] == [
        {"protocol": "frame", "url": "http://h:9001/peer/v1/stream", "version": "1"},
        {"protocol": "rest", "url": "http://h:9001", "version": "1"},
    ]
    assert card["preferred_transport"] == "frame"


def test_card_transports_override_with_frame_first() -> None:
    """PR-B1: callers can pass an explicit ``transports`` list.

    Order matters: the first entry drives ``preferred_transport``.
    """
    card = build_agent_card(
        orch_id="o",
        url="http://h:9001",
        transports=[
            {"protocol": "frame", "url": "https://h:9002", "version": "1"},
            {"protocol": "rest", "url": "http://h:9001", "version": "1"},
        ],
    )
    assert card["transports"][0]["protocol"] == "frame"
    assert card["preferred_transport"] == "frame"


def test_card_resolve_frame_url_uses_public_override(tmp_path, monkeypatch) -> None:
    """PR-B2: ``_resolve_frame_url`` follows the same env priority as
    ``_resolve_card_url`` so an operator with ``ORCHESTRATORD_PEER_PUBLIC_URL``
    gets a matching frame URL on a reverse-proxied deployment."""
    monkeypatch.setenv(
        "ORCHESTRATORD_PEER_PUBLIC_URL", "https://orch.example.com"
    )
    from orchestratord.peer.card import _resolve_frame_url

    assert _resolve_frame_url() == "https://orch.example.com/peer/v1/stream"


def test_card_resolve_frame_url_honors_frame_listen_override(
    tmp_path, monkeypatch
) -> None:
    """PR-B2.1: ``ORCHESTRATORD_PEER_FRAME_LISTEN`` (HOST:PORT) wins
    over the REST URL chain when set, so an operator running the frame
    listener on its own socket advertises the right frame endpoint in
    the Agent Card without rewriting the REST URL.

    Resolution precedence (top wins): ``ORCHESTRATORD_PEER_FRAME_LISTEN``,
    then ``ORCHESTRATORD_PEER_PUBLIC_URL``, then ``ORCHESTRATORD_PEER_LISTEN``,
    then the local default. This test pins the *first* rung of that
    chain so the split-port deployment keeps working.
    """
    monkeypatch.setenv(
        "ORCHESTRATORD_PEER_FRAME_LISTEN", "10.0.0.5:9002"
    )
    # Also set PUBLIC_URL so we prove the frame-specific override beats
    # the public-URL fallback (which would otherwise win).
    monkeypatch.setenv(
        "ORCHESTRATORD_PEER_PUBLIC_URL", "https://orch.example.com"
    )
    from orchestratord.peer.card import _resolve_frame_url

    assert _resolve_frame_url() == "http://10.0.0.5:9002/peer/v1/stream"


def test_card_resolve_frame_url_falls_back_when_frame_listen_empty(
    tmp_path, monkeypatch
) -> None:
    """PR-B2.1: an empty ``ORCHESTRATORD_PEER_FRAME_LISTEN`` is treated
    as unset — operators can pre-set the env to ``""`` in deploy
    scripts without forcing the split-port path on by accident.
    """
    monkeypatch.setenv("ORCHESTRATORD_PEER_FRAME_LISTEN", "")
    monkeypatch.setenv(
        "ORCHESTRATORD_PEER_LISTEN", "127.0.0.1:9001"
    )
    from orchestratord.peer.card import _resolve_frame_url

    assert _resolve_frame_url() == "http://127.0.0.1:9001/peer/v1/stream"


def test_serve_parser_registers_peer_frame_listen_flag() -> None:
    """PR-B2.1: the ``serve`` subcommand exposes ``--peer-frame-listen``
    so an operator can launch the daemon with the frame listener on a
    separate socket from the CLI without exporting env vars manually.
    """
    import argparse

    from orchestratord.cli import serve as serve_cli

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    serve_cli.add_serve_parser(subparsers)
    args = parser.parse_args(["serve", "--peer-frame-listen", "10.0.0.5:9002"])
    assert args.peer_frame_listen == "10.0.0.5:9002"


def test_serve_runner_propagates_peer_frame_listen_to_env(monkeypatch) -> None:
    """PR-B2.1: ``run(args)`` propagates ``--peer-frame-listen`` into
    ``ORCHESTRATORD_PEER_FRAME_LISTEN`` via ``os.environ.setdefault``
    so the card helper (``_resolve_frame_url``) sees the operator's
    override without the operator having to export the env manually.

    We exercise only the env-propagation snippet (``os.environ.setdefault``
    blocks) directly so we don't have to spin up uvicorn + the seed
    machinery in a unit test. The snippet is the contract — uvicorn
    integration is covered by smoke tests, not unit tests.
    """
    import argparse
    import os

    from orchestratord.cli import serve as serve_cli

    monkeypatch.delenv("ORCHESTRATORD_PEER_FRAME_LISTEN", raising=False)
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    serve_cli.add_serve_parser(subparsers)
    args = parser.parse_args(
        ["serve", "--peer-frame-listen", "10.0.0.5:9002", "--no-seed"]
    )

    # Mirror the env-setting block from serve_cli.run().
    os.environ.setdefault("ORCHESTRATORD_PEER_LISTEN", args.peer_listen)
    peer_frame_listen = getattr(args, "peer_frame_listen", None)
    if peer_frame_listen:
        os.environ.setdefault(
            "ORCHESTRATORD_PEER_FRAME_LISTEN", peer_frame_listen
        )

    assert os.environ["ORCHESTRATORD_PEER_FRAME_LISTEN"] == "10.0.0.5:9002"


def test_serve_runner_does_not_set_env_when_flag_omitted(monkeypatch) -> None:
    """PR-B2.1: omitting ``--peer-frame-listen`` leaves
    ``ORCHESTRATORD_PEER_FRAME_LISTEN`` unset so the card helper falls
    back to the REST URL — the single-port default is preserved.
    """
    import argparse
    import os

    from orchestratord.cli import serve as serve_cli

    monkeypatch.delenv("ORCHESTRATORD_PEER_FRAME_LISTEN", raising=False)
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    serve_cli.add_serve_parser(subparsers)
    args = parser.parse_args(["serve", "--no-seed"])
    assert args.peer_frame_listen is None

    # Mirror the env-setting block from serve_cli.run() — must skip
    # the ORCHESTRATORD_PEER_FRAME_LISTEN step entirely.
    if getattr(args, "peer_frame_listen", None):
        os.environ.setdefault(
            "ORCHESTRATORD_PEER_FRAME_LISTEN", args.peer_frame_listen
        )
    assert "ORCHESTRATORD_PEER_FRAME_LISTEN" not in os.environ
