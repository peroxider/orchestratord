"""Create UNIQUE INDEX CONCURRENTLY on agents(workspace_id, name) (§6.1)."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_agents_workspace_name",
            "agents",
            ["workspace_id", "name"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_agents_workspace_name",
            table_name="agents",
            postgresql_concurrently=True,
        )
