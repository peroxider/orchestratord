"""Create the §6.1 messages table for workspace-level chat.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-07

Indexes are intentionally absent here: per §6.1 every index is a
``CREATE INDEX CONCURRENTLY`` / ``CREATE UNIQUE INDEX CONCURRENTLY`` in its own
migration file (0044). No ``ForeignKey`` is declared anywhere — referential
integrity is app-layer only (§6.1).
"""

from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("author_label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("messages")
