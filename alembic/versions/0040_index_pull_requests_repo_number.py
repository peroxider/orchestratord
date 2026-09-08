"""Create UNIQUE INDEX CONCURRENTLY on pull_requests(repo, number) (§6.5, §6.1)."""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_pull_requests_repo_number",
            "pull_requests",
            ["repo", "number"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_pull_requests_repo_number",
            table_name="pull_requests",
            postgresql_concurrently=True,
        )
