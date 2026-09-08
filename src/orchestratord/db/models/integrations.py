"""Notification-channel integration model (§7.5).

``integrations`` records one OAuth integration per workspace per provider
(Slack / Lark), holding the inbound-webhook URL the adapters POST to. It is the
persistent side of the §7.5 OAuth handshake; the ``channels`` table holds the
N channel bindings within an integration.

Per §6.1 no model declares a ``ForeignKey`` and no lookup index is created
inline — those are emitted as separate ``CREATE INDEX CONCURRENTLY``
migrations. The uniqueness invariant (one integration per
``(workspace_id, provider)``) is declared as an inline ``UniqueConstraint`` so
the dev/test ``create_all`` schema enforces it; production applies the
same-named UNIQUE index via migration 0042.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider", name="uq_integrations_workspace_provider"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    provider: Mapped[str]
    webhook_url: Mapped[str]
    created_at: Mapped[datetime]
