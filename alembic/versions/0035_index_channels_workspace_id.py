"""Create INDEX CONCURRENTLY on channels(workspace_id) (§6.1)."""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_channels_workspace_id",
            "channels",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_channels_workspace_id",
            table_name="channels",
            postgresql_concurrently=True,
        )
