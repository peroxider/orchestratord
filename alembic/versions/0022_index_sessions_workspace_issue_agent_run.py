"""Create INDEX CONCURRENTLY on sessions(workspace_id, issue_id, agent_id, run_id) (§6.1)."""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_sessions_workspace_issue_agent_run",
            "sessions",
            ["workspace_id", "issue_id", "agent_id", "run_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_sessions_workspace_issue_agent_run",
            table_name="sessions",
            postgresql_concurrently=True,
        )
