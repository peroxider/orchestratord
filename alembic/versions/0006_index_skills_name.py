"""Create UNIQUE INDEX CONCURRENTLY on skills.name (§6.1)."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_skills_name",
            "skills",
            ["name"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_skills_name",
            table_name="skills",
            postgresql_concurrently=True,
        )
