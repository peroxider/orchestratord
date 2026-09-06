"""Create UNIQUE INDEX CONCURRENTLY on approvals(session_id, request_id) (§6.1)."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_approvals_session_request",
            "approvals",
            ["session_id", "request_id"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_approvals_session_request",
            table_name="approvals",
            postgresql_concurrently=True,
        )
