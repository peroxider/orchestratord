"""Create UNIQUE INDEX CONCURRENTLY on integrations(workspace_id, provider) (§7.5, §6.1)."""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_integrations_workspace_provider",
            "integrations",
            ["workspace_id", "provider"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_integrations_workspace_provider",
            table_name="integrations",
            postgresql_concurrently=True,
        )
