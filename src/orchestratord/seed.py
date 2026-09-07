"""§10.2 first-boot seed — single-user default tenancy.

``orchestratord serve`` runs :func:`seed_default_workspace` once per
database (spec §10.2): a ``default`` workspace, one owner member with a
FIXED UUID (so automations and docs can reference it), and one daemon
runtime token whose plaintext is returned exactly once — only the
sha256 hash is persisted.

Idempotent: a re-run is a no-op when the ``default`` workspace already
exists. Operators opt out with ``ORCHESTRATORD_SKIP_SEED=1`` or the
serve ``--no-seed`` flag.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from orchestratord.db.engine import build_session_factory
from orchestratord.db.models.audit_auth import AuthToken
from orchestratord.db.models.tenancy import Member, Workspace

DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_WORKSPACE_NAME = "Default"
DEFAULT_OWNER_MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_OWNER_NAME = "Owner"
DAEMON_TOKEN_NAME = "daemon-runtime"
DAEMON_TOKEN_SCOPES = ["runtime"]


@dataclass(frozen=True)
class SeedResult:
    """What one successful seed created — ``token_plaintext`` is one-time."""

    workspace_id: uuid.UUID
    member_id: uuid.UUID
    token_plaintext: str


def hash_token(plaintext: str) -> str:
    """sha256 hex digest — the only form ever persisted (§10.2)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def seed_default_workspace(session_factory) -> SeedResult | None:
    """Seed the default tenancy or return ``None`` when already present."""
    async with session_factory() as db:
        existing = await db.scalar(
            select(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
        )
        if existing is not None:
            return None

        now = datetime.now(UTC)
        workspace = Workspace(
            id=uuid.uuid4(),
            slug=DEFAULT_WORKSPACE_SLUG,
            name=DEFAULT_WORKSPACE_NAME,
            created_at=now,
        )
        member = Member(
            id=DEFAULT_OWNER_MEMBER_ID,
            workspace_id=workspace.id,
            role="owner",
            name=DEFAULT_OWNER_NAME,
            created_at=now,
        )
        plaintext = secrets.token_urlsafe(32)
        token = AuthToken(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            name=DAEMON_TOKEN_NAME,
            token_hash=hash_token(plaintext),
            scopes=list(DAEMON_TOKEN_SCOPES),
            expires_at=None,
            created_at=now,
        )
        db.add_all([workspace, member, token])
        await db.commit()
        return SeedResult(
            workspace_id=workspace.id,
            member_id=member.id,
            token_plaintext=plaintext,
        )


def run_seed() -> SeedResult | None:
    """Sync entry point used by ``orchestratord serve``."""
    return asyncio.run(seed_default_workspace(build_session_factory()))
