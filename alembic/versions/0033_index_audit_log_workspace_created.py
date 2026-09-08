"""Create INDEX CONCURRENTLY on audit_log(workspace_id, created_at DESC) (§6.1)."""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_audit_log_workspace_created "
            "ON audit_log (workspace_id, created_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY ix_audit_log_workspace_created")
