"""API-token entity + issuance contract (``docs/FEATURE_GAP_VS_MULTICA.md``
§5.7.4, §6.1.1).

A multica-style API token grants the Web client / automation scoped access to
a workspace. It mirrors the Runtime token contract (§6.3): the plaintext is
issued exactly once and only the SHA-256 hash is stored on the entity;
``expires_at`` bounds validity.

Invariants:

* ``issue_api_token`` returns ``(plaintext, hash)`` with ``hash == sha256(plaintext)``.
* ``verify_api_token`` accepts the matching plaintext and rejects expired tokens.
* ``expires_at=None`` means never-expiring.
* ``created_at`` / ``expires_at`` are timezone-aware.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.4, §6.1.1.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID


def hash_api_token(plaintext: str) -> str:
    """One-way hash of an API token. Plaintext is never stored."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_api_token() -> tuple[str, str]:
    """Issue a fresh ``(plaintext, hash)`` token pair.

    The plaintext lives only in the caller's response; only the hash is
    persisted.
    """
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_api_token(plaintext)


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class AuthToken:
    id: UUID
    workspace_id: UUID
    name: str
    token_hash: str
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(UTC)
        self.created_at = _normalize_dt(self.created_at)
        self.expires_at = _normalize_dt(self.expires_at)

    def is_expired(self) -> bool:
        """Return whether the token has passed its ``expires_at`` bound."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at


def verify_api_token(token: AuthToken, plaintext: str) -> bool:
    """Return whether *plaintext* matches *token*'s hash and is not expired."""
    if token.is_expired():
        return False
    return hash_api_token(plaintext) == token.token_hash


__all__ = ["AuthToken", "hash_api_token", "issue_api_token", "verify_api_token"]
