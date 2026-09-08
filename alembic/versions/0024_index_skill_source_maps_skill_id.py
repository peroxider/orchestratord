"""Create INDEX CONCURRENTLY on skill_source_maps(skill_id) (§6.1)."""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_skill_source_maps_skill_id",
            "skill_source_maps",
            ["skill_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_skill_source_maps_skill_id",
            table_name="skill_source_maps",
            postgresql_concurrently=True,
        )
