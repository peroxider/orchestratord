"""Create INDEX on events(session_id, sequence) (§6.1.2).

``events`` is a partitioned parent (PARTITION BY RANGE created_at), and
Postgres rejects ``CREATE INDEX CONCURRENTLY`` on partitioned tables (same
reason as 0009). At migration time the parent has no partitions, so a plain
CREATE INDEX is instant; partitions attached later inherit the index
automatically.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_events_session_sequence",
        "events",
        ["session_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_events_session_sequence",
        table_name="events",
    )
