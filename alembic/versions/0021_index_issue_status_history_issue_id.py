"""Create INDEX CONCURRENTLY on issue_status_history(issue_id) (§6.1)."""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_issue_status_history_issue_id",
            "issue_status_history",
            ["issue_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_issue_status_history_issue_id",
            table_name="issue_status_history",
            postgresql_concurrently=True,
        )
