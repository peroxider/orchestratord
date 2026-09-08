"""Create INDEX CONCURRENTLY on projects(workspace_id) (§6.1)."""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_projects_workspace_id",
            "projects",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_projects_workspace_id",
            table_name="projects",
            postgresql_concurrently=True,
        )
