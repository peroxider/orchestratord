"""PR-B6 TLS tests: client trust resolution, serve TLS flags, and the
self-signed cert generation script.

* :func:`orchestratord.peer.transports.peer_tls_verify` — env-driven
  trust resolution (system store → CA bundle → insecure).
* ``orchestratord serve --tls-certfile/--tls-keyfile`` argparse surface.
* ``scripts/gen_peer_tls_certs.sh`` produces a chain that
  ``openssl verify`` accepts with the expected SANs (skipped when
  openssl/bash are unavailable).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from orchestratord.peer.transports import peer_tls_verify


@pytest.fixture(autouse=True)
def _clean_tls_env(monkeypatch):
    """Isolate the TLS env knobs so ambient operator env can't leak in."""
    monkeypatch.delenv("ORCHESTRATORD_PEER_TLS_CA", raising=False)
    monkeypatch.delenv("ORCHESTRATORD_PEER_TLS_INSECURE", raising=False)


def test_peer_tls_verify_defaults_to_system_trust() -> None:
    """No env knobs → httpx default (system trust store)."""
    assert peer_tls_verify() is True


def test_peer_tls_verify_trusts_ca_bundle(monkeypatch, tmp_path) -> None:
    """ORCHESTRATORD_PEER_TLS_CA → the path is handed to httpx verify."""
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("ORCHESTRATORD_PEER_TLS_CA", str(ca))
    assert peer_tls_verify() == str(ca)


def test_peer_tls_insecure_wins_and_warns(monkeypatch, tmp_path, caplog) -> None:
    """INSECURE=1 disables verification (dev only) even when a CA is
    configured, and must log a loud warning — never silent."""
    import logging

    monkeypatch.setenv("ORCHESTRATORD_PEER_TLS_CA", str(tmp_path / "ca.pem"))
    monkeypatch.setenv("ORCHESTRATORD_PEER_TLS_INSECURE", "1")
    with caplog.at_level(logging.WARNING):
        assert peer_tls_verify() is False
    assert any(
        "ORCHESTRATORD_PEER_TLS_INSECURE" in record.message
        for record in caplog.records
    )


def test_serve_parser_accepts_tls_flags() -> None:
    """``serve --tls-certfile/--tls-keyfile`` parse into args (PR-B6)."""
    import argparse

    from orchestratord.cli.serve import add_serve_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_serve_parser(subparsers)
    args = parser.parse_args(
        [
            "serve",
            "--tls-certfile", "/pki/server.crt",
            "--tls-keyfile", "/pki/server.key",
        ]
    )
    assert args.tls_certfile == "/pki/server.crt"
    assert args.tls_keyfile == "/pki/server.key"


def test_gen_peer_tls_certs_script_produces_verifiable_chain(
    tmp_path,
) -> None:
    """The PR-B6 script generates a CA + server cert whose chain
    verifies, with SANs covering the requested host and loopback."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "gen_peer_tls_certs.sh"
    if not script.exists():
        pytest.skip("gen_peer_tls_certs.sh not found")
    for tool in ("openssl", "bash"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not available")

    out_dir = tmp_path / "pki"
    subprocess.run(
        ["bash", str(script), "--out-dir", str(out_dir),
         "--hostname", "orch-b.example.com"],
        check=True,
        capture_output=True,
    )

    ca = out_dir / "ca.pem"
    server_crt = out_dir / "server.crt"
    server_key = out_dir / "server.key"
    assert ca.exists() and server_crt.exists() and server_key.exists()
    # Chain verification — the core property peers rely on.
    subprocess.run(
        ["openssl", "verify", "-CAfile", str(ca), str(server_crt)],
        check=True,
        capture_output=True,
    )
    # SANs: requested host + loopback defaults.
    san = subprocess.run(
        ["openssl", "x509", "-in", str(server_crt), "-noout",
         "-ext", "subjectAltName"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "DNS:orch-b.example.com" in san
    assert "DNS:localhost" in san
    assert "127.0.0.1" in san


def test_gen_peer_tls_certs_script_rejects_unknown_args() -> None:
    """Unknown flags exit non-zero (usage guard)."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "gen_peer_tls_certs.sh"
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    result = subprocess.run(
        ["bash", str(script), "--bogus-flag"],
        capture_output=True,
    )
    assert result.returncode != 0
