"""Create UNIQUE INDEX CONCURRENTLY on usage_aggregates(workspace_id, agent_id,
issue_id, backend, day) (§5.2.4, §6.1).

The usage_aggregates table pre-aggregates at the finest grouping granularity the
dashboard's dimension switch (§5.2.4: workspace / agent / issue / backend) and
daily line chart need.  ``agent_id`` / ``issue_id`` are nullable ("unassigned"),
so the index is declared ``NULLS NOT DISTINCT`` to treat two unassigned values as
the same bucket.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_usage_aggregates_bucket",
            "usage_aggregates",
            ["workspace_id", "agent_id", "issue_id", "backend", "day"],
            unique=True,
            postgresql_concurrently=True,
            postgresql_nulls_not_distinct=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_usage_aggregates_bucket",
            table_name="usage_aggregates",
            postgresql_concurrently=True,
        )
