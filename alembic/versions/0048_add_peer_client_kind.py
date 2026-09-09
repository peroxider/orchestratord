"""Add ``peers.client_kind`` for Phase B (PR-B1, D26 follow-up).

Each row carries a stable client-version tag so the operator can see which
peers still speak the Phase 1 wire format (``v1_sunset``) versus the
Phase B v2 format that includes ``transports[]`` and
``peer_client_version``. Existing rows adopt ``v1_sunset`` on upgrade —
they predate the field, so they are by definition on the legacy path.
``server_default`` is kept (no follow-up ``alter_column``) so future
Phase 1 clients that omit the column are still classified correctly.
"""

import sqlalchemy as sa

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "peers",
        sa.Column(
            "client_kind",
            sa.Text(),
            nullable=False,
            server_default="v1_sunset",
        ),
    )


def downgrade() -> None:
    op.drop_column("peers", "client_kind")