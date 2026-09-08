"""Create INDEX CONCURRENTLY on agents(runtime_id) (§6.1)."""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_agents_runtime_id",
            "agents",
            ["runtime_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_agents_runtime_id",
            table_name="agents",
            postgresql_concurrently=True,
        )
