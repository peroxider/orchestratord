"""Create INDEX CONCURRENTLY on events(workspace_id, created_at DESC) (§6.1.2)."""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_events_workspace_created "
            "ON events (workspace_id, created_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY ix_events_workspace_created")
