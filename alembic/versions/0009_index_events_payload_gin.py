"""Create GIN index on events.payload (jsonb_path_ops) (§6.1.2).

``events`` is a partitioned parent (PARTITION BY RANGE created_at), and
Postgres rejects ``CREATE INDEX CONCURRENTLY`` on partitioned tables. At
migration time the parent has no partitions (they are attached lazily per
month at insert time, see db/partitions.py), so a plain CREATE INDEX is
instant; partitions attached later inherit the parent's partitioned index
automatically.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_events_payload_gin",
        "events",
        ["payload"],
        postgresql_using="gin",
        postgresql_ops={"payload": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_events_payload_gin",
        table_name="events",
    )
