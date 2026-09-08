"""Create INDEX CONCURRENTLY on runtimes(workspace_id) (§6.1)."""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_runtimes_workspace_id",
            "runtimes",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_runtimes_workspace_id",
            table_name="runtimes",
            postgresql_concurrently=True,
        )
