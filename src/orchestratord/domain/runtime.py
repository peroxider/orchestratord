"""Runtime entity + token contract (§6.3, §5.7.4).

A Runtime represents a host machine that has run
``orchestratord daemon start --workspace-token ...`` and is connected to the
server over WebSocket. Security invariants enforced at the model layer:

* Token storage is one-way hashed — plaintext never appears on the entity.
* ``issue_runtime_token`` / ``hash_runtime_token`` / ``verify_runtime_token``
  form the issuance round-trip; rotation issues a fresh token, revocation
  flips ``status`` to ``DISABLED`` and bars subsequent verification.
* Heartbeats drive ``last_seen_at``; staleness is enforced via ``is_online``.
"""

from __future__ import annotations

import enum
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID


class RuntimeStatus(enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DISABLED = "disabled"


def hash_runtime_token(plaintext: str) -> str:
    """One-way hash of a runtime token. Plaintext is never stored."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_runtime_token() -> tuple[str, str]:
    """Issue a fresh ``(plaintext, hash)`` token pair.

    The plaintext lives only in the caller's shell environment; only the
    hash is persisted.
    """
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_runtime_token(plaintext)


def verify_runtime_token(runtime: Runtime, plaintext: str) -> bool:
    """Return whether *plaintext* matches *runtime*'s stored hash.

    A disabled runtime always rejects — revocation closes the WS and bars
    subsequent token use.
    """
    if runtime.status is RuntimeStatus.DISABLED:
        return False
    return hash_runtime_token(plaintext) == runtime.token_hash


@dataclass
class Runtime:
    id: UUID
    workspace_id: UUID
    hostname: str
    os: str
    token_hash: str
    status: RuntimeStatus = RuntimeStatus.ONLINE
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    probed_backends: list[dict] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(UTC)

    def is_online(self, threshold: timedelta) -> bool:
        """Return whether the runtime's last heartbeat is within *threshold*."""
        if self.status is RuntimeStatus.DISABLED:
            return False
        if self.last_seen_at is None:
            return False
        age = datetime.now(UTC) - self.last_seen_at
        return age <= threshold

    def revoke(self) -> None:
        """Revoke the runtime: flip status to DISABLED (closes its WS)."""
        self.status = RuntimeStatus.DISABLED

    def record_probed_backends(self, backends: list[dict]) -> None:
        """Record the CLI list the runtime probed on its host (§6.3)."""
        self.probed_backends = list(backends)
