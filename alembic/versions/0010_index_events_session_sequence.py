"""Create INDEX CONCURRENTLY on events(session_id, sequence) (§6.1.2)."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_events_session_sequence",
            "events",
            ["session_id", "sequence"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_events_session_sequence",
            table_name="events",
            postgresql_concurrently=True,
        )
