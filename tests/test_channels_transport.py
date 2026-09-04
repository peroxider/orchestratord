"""validate_webhook_url / redact_webhook_url / encode_json_body tests."""

from __future__ import annotations

import json
import socket

import pytest

from orchestratord.channels import (
    InvalidWebhookURLError,
    encode_json_body,
    redact_webhook_url,
    validate_webhook_url,
)


def test_validate_webhook_url_accepts_https_with_public_host() -> None:
    # Use a hostname that resolves to a public IP; the helper must accept it.
    url = "https://hooks.example.com/services/abcdef0123456789"
    # If DNS lookup fails in the sandbox we still want the unit test to
    # pass; that's why the helper exposes ``resolve_host=False``.
    assert (
        validate_webhook_url(url, resolve_host=False)
        == "https://hooks.example.com/services/abcdef0123456789"
    )


def test_validate_webhook_url_rejects_http_by_default() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url(
            "http://hooks.example.com/x",
            resolve_host=False,
        )


def test_validate_webhook_url_accepts_http_with_allow_http() -> None:
    url = "http://hooks.example.com/x"
    assert validate_webhook_url(url, allow_http=True, resolve_host=False) == url


def test_validate_webhook_url_rejects_empty() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("")


def test_validate_webhook_url_rejects_non_string() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url(None)  # type: ignore[arg-type]


def test_validate_webhook_url_rejects_unknown_scheme() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("ftp://hooks.example.com/x", resolve_host=False)


def test_validate_webhook_url_rejects_empty_hostname() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https:///path", resolve_host=False)


def test_validate_webhook_url_rejects_loopback_literal_by_default() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://127.0.0.1/x", resolve_host=False)


def test_validate_webhook_url_rejects_loopback_hostname_by_default() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://localhost/x", resolve_host=False)


def test_validate_webhook_url_accepts_loopback_with_allow_loopback() -> None:
    url = "https://localhost/x"
    assert validate_webhook_url(url, allow_loopback=True, resolve_host=False) == url


def test_validate_webhook_url_rejects_link_local_literal() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://169.254.169.254/x", resolve_host=False)


def test_validate_webhook_url_rejects_multicast_literal() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://224.0.0.1/x", resolve_host=False)


def test_validate_webhook_url_rejects_unspecified_literal() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://0.0.0.0/x", resolve_host=False)


def test_validate_webhook_url_rejects_private_literal() -> None:
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://10.0.0.5/x", resolve_host=False)
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://192.168.1.1/x", resolve_host=False)


def test_validate_webhook_url_accepts_allow_loopback_for_private() -> None:
    url = "https://10.0.0.5/x"
    assert validate_webhook_url(url, allow_loopback=True, resolve_host=False) == url


def test_validate_webhook_url_rejects_unresolvable_host(monkeypatch) -> None:
    # Make resolution raise gaierror so the DNS failure path is exercised.
    def _raise(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)

    # A DNS name that does not resolve must raise InvalidWebhookURLError
    # (not socket.gaierror leaking through).
    with pytest.raises(InvalidWebhookURLError):
        validate_webhook_url("https://this-host-should-not-exist-orchestratord-12345.example/x")


def test_redact_webhook_url_strips_query_string() -> None:
    redacted = redact_webhook_url(
        "https://hooks.example.com/services/T0/B0/abcdef0123456789?token=secret"
    )
    assert "token=secret" not in redacted
    assert "?" not in redacted


def test_redact_webhook_url_masks_token_path_segment() -> None:
    redacted = redact_webhook_url("https://hooks.example.com/services/T0/B0/abcdef0123456789")
    assert redacted.endswith("/***")
    assert "abcdef0123456789" not in redacted


def test_redact_webhook_url_preserves_scheme_and_host() -> None:
    redacted = redact_webhook_url("https://hooks.example.com/api/webhooks/1/abcdef0123456789")
    assert redacted.startswith("https://hooks.example.com/api/webhooks/1/")


def test_redact_webhook_url_handles_non_token_path() -> None:
    # Path segments shorter than 6 alphanumeric chars are kept verbatim.
    url = "https://hooks.example.com/api/v1/run"
    assert redact_webhook_url(url) == url


def test_redact_webhook_url_empty_input() -> None:
    assert redact_webhook_url("") == ""
    assert redact_webhook_url(None) == ""  # type: ignore[arg-type]


def test_encode_json_body_compact_and_unicode_safe() -> None:
    payload = {"text": "你好", "level": "info"}
    body = encode_json_body(payload)
    # Compact separators: no spaces between separators.
    assert b", " not in body
    assert b": " not in body
    # Unicode preserved (ensure_ascii=False).
    assert "你好".encode() in body
    # And it must round-trip back to the original dict.
    assert json.loads(body.decode("utf-8")) == payload
