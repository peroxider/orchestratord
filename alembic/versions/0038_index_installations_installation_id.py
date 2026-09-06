"""Create UNIQUE INDEX CONCURRENTLY on installations(installation_id) (§6.5, §6.1)."""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_installations_installation_id",
            "installations",
            ["installation_id"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_installations_installation_id",
            table_name="installations",
            postgresql_concurrently=True,
        )
