"""Create UNIQUE INDEX CONCURRENTLY on agent_capabilities_cache(agent_id) (§6.1)."""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_agent_capabilities_cache_agent_id",
            "agent_capabilities_cache",
            ["agent_id"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_agent_capabilities_cache_agent_id",
            table_name="agent_capabilities_cache",
            postgresql_concurrently=True,
        )
