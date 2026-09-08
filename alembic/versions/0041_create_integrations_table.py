"""Create the §7.5 integrations table.

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-06

Indexes are intentionally absent here: per §6.1 every index is a
``CREATE INDEX CONCURRENTLY`` / ``CREATE UNIQUE INDEX CONCURRENTLY`` in its own
migration file (``0042``). No ``ForeignKey`` is declared anywhere — referential
integrity is app-layer only (§6.1).
"""

from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("integrations")
