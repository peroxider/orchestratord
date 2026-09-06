"""Create UNIQUE INDEX CONCURRENTLY on auth_tokens.token_hash (§6.1)."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_auth_tokens_token_hash",
            "auth_tokens",
            ["token_hash"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_auth_tokens_token_hash",
            table_name="auth_tokens",
            postgresql_concurrently=True,
        )
