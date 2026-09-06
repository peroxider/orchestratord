"""Create INDEX CONCURRENTLY on inbox(workspace_id) (§6.1)."""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_inbox_workspace_id",
            "inbox",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_inbox_workspace_id",
            table_name="inbox",
            postgresql_concurrently=True,
        )
