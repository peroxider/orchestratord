"""Create INDEX CONCURRENTLY on autopilots(workspace_id) (§6.1)."""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_autopilots_workspace_id",
            "autopilots",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_autopilots_workspace_id",
            table_name="autopilots",
            postgresql_concurrently=True,
        )
