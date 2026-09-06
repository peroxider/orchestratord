"""Create INDEX CONCURRENTLY on pull_requests(issue_id) (§6.5, §6.1)."""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_pull_requests_issue_id",
            "pull_requests",
            ["issue_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_pull_requests_issue_id",
            table_name="pull_requests",
            postgresql_concurrently=True,
        )
