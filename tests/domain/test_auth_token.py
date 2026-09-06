"""API-token entity invariants (§5.7.4).

``AuthToken`` mirrors the Runtime token contract (§6.3): plaintext is issued
once and only the SHA-256 hash is stored; ``verify_api_token`` rejects expired
tokens. Invariants:

* ``issue_api_token`` returns ``(plaintext, hash)`` with ``hash == sha256(plaintext)``.
* ``verify_api_token`` accepts the matching plaintext, rejects anything else.
* ``is_expired`` treats ``expires_at=None`` as never-expiring.
* ``created_at`` / ``expires_at`` are timezone-aware.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from orchestratord.domain.auth_token import (
    AuthToken,
    hash_api_token,
    issue_api_token,
    verify_api_token,
)


def _token(**overrides) -> AuthToken:
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "name": "ci",
        "token_hash": hash_api_token("sekret"),
    }
    defaults.update(overrides)
    return AuthToken(**defaults)


class TestIssuance:
    def test_issue_returns_plaintext_and_hash(self) -> None:
        plaintext, token_hash = issue_api_token()
        assert plaintext != token_hash
        assert token_hash == hash_api_token(plaintext)

    def test_issue_is_unique(self) -> None:
        plaintext_a, hash_a = issue_api_token()
        plaintext_b, hash_b = issue_api_token()
        assert plaintext_a != plaintext_b
        assert hash_a != hash_b


class TestVerify:
    def test_matching_plaintext_verifies(self) -> None:
        token = _token(token_hash=hash_api_token("sekret"))
        assert verify_api_token(token, "sekret") is True

    def test_wrong_plaintext_rejected(self) -> None:
        token = _token(token_hash=hash_api_token("sekret"))
        assert verify_api_token(token, "nope") is False

    def test_expired_token_rejected(self) -> None:
        token = _token(
            token_hash=hash_api_token("sekret"),
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        assert verify_api_token(token, "sekret") is False


class TestExpiry:
    def test_none_never_expires(self) -> None:
        assert _token().is_expired() is False

    def test_past_expires(self) -> None:
        token = _token(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        assert token.is_expired() is True

    def test_future_not_expired(self) -> None:
        token = _token(expires_at=datetime.now(UTC) + timedelta(minutes=1))
        assert token.is_expired() is False


class TestFields:
    def test_defaults(self) -> None:
        token = _token()
        assert token.scopes == []
        assert token.expires_at is None
        assert token.created_at.tzinfo is not None

    def test_naive_expires_at_normalized(self) -> None:
        naive = datetime.fromisoformat("2026-01-01T00:00:00")
        token = _token(expires_at=naive)
        assert token.expires_at.tzinfo is UTC
