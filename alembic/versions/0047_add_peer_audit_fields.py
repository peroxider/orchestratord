"""Add peer-invite provenance columns to audit_log (DESIGN D17 / ADR D13).

``POST /api/peer/peers/{orch-id}/sessions`` (cross-daemon session
creation, PR5) stamps ``invited_by_orch_id`` and
``invited_by_peer_call_id`` so the creating daemon is traceable
end-to-end (§7 R7 cross-daemon audit stitching). Both are nullable —
only peer-initiated mutations carry them.
"""

import sqlalchemy as sa

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("invited_by_orch_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "audit_log",
        sa.Column("invited_by_peer_call_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_log", "invited_by_peer_call_id")
    op.drop_column("audit_log", "invited_by_orch_id")
