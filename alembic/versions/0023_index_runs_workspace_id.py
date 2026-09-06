"""Create INDEX CONCURRENTLY on runs(workspace_id) (§6.1)."""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_runs_workspace_id",
            "runs",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_runs_workspace_id",
            table_name="runs",
            postgresql_concurrently=True,
        )
