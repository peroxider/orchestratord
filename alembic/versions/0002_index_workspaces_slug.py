"""Create UNIQUE INDEX CONCURRENTLY on workspaces.slug (§6.1)."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_workspaces_slug",
            "workspaces",
            ["slug"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_workspaces_slug",
            table_name="workspaces",
            postgresql_concurrently=True,
        )
