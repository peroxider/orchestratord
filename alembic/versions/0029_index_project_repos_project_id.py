"""Create INDEX CONCURRENTLY on project_repos(project_id) (§6.1)."""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_project_repos_project_id",
            "project_repos",
            ["project_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_project_repos_project_id",
            table_name="project_repos",
            postgresql_concurrently=True,
        )
