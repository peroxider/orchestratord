"""Create INDEX CONCURRENTLY on auth_tokens(workspace_id) (§6.1)."""

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_auth_tokens_workspace_id",
            "auth_tokens",
            ["workspace_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_auth_tokens_workspace_id",
            table_name="auth_tokens",
            postgresql_concurrently=True,
        )
