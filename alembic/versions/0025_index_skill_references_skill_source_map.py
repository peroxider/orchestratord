"""Create INDEX CONCURRENTLY on skill_references(skill_id, source_map_id) (§6.1)."""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_skill_references_skill_source_map",
            "skill_references",
            ["skill_id", "source_map_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_skill_references_skill_source_map",
            table_name="skill_references",
            postgresql_concurrently=True,
        )
