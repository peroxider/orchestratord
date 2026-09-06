"""Create INDEX CONCURRENTLY on runtime_backends(runtime_id) (§6.1)."""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_runtime_backends_runtime_id",
            "runtime_backends",
            ["runtime_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_runtime_backends_runtime_id",
            table_name="runtime_backends",
            postgresql_concurrently=True,
        )
