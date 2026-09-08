"""Create INDEX CONCURRENTLY on squads(workspace_id) (§6.1)."""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_squads_workspace_id",
            "squads",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_squads_workspace_id",
            table_name="squads",
            postgresql_concurrently=True,
        )
