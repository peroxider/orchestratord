"""Create INDEX CONCURRENTLY on issues(assignee_type, assignee_id) (§6.1)."""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_issues_assignee_type_id",
            "issues",
            ["assignee_type", "assignee_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_issues_assignee_type_id",
            table_name="issues",
            postgresql_concurrently=True,
        )
