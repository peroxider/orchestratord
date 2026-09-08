"""Create INDEX CONCURRENTLY on installations(workspace_id) (§6.5, §6.1)."""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_installations_workspace_id",
            "installations",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_installations_workspace_id",
            table_name="installations",
            postgresql_concurrently=True,
        )
