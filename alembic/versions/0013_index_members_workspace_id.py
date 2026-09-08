"""Create INDEX CONCURRENTLY on members(workspace_id) (§6.1)."""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_members_workspace_id",
            "members",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_members_workspace_id",
            table_name="members",
            postgresql_concurrently=True,
        )
