"""Create the peers table for the Peer Federation registry (ADR-001 D16).

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-08

No ForeignKeys (app-layer integrity, §6.1 house rule) and no secondary
indexes: peer lookups are (workspace_id, orch_id) on a small table.
The one constraint is ``uq_peers_workspace_orch`` — the registry's
documented key must be DB-enforced so two concurrent invite POSTs
cannot both read-absent and insert (which would leave every later
``get_peer`` for that key raising ``MultipleResultsFound``).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "peers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("orch_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("remote_workspace_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("token_id", sa.Uuid(), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("card", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "orch_id", name="uq_peers_workspace_orch"
        ),
    )


def downgrade() -> None:
    op.drop_table("peers")
