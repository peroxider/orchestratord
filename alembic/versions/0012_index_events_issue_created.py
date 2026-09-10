"""Create INDEX on events(issue_id, created_at DESC) (§6.1.2).

``events`` is a partitioned parent (PARTITION BY RANGE created_at), and
Postgres rejects ``CREATE INDEX CONCURRENTLY`` on partitioned tables (same
reason as 0009). At migration time the parent has no partitions, so a plain
CREATE INDEX is instant; partitions attached later inherit the index
automatically.
"""

from alembic import op
from sqlalchemy import text as sa_text

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_events_issue_created",
        "events",
        ["issue_id", sa_text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_events_issue_created",
        table_name="events",
    )
