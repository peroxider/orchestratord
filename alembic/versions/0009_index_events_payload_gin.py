"""Create GIN index CONCURRENTLY on events.payload (jsonb_path_ops) (§6.1.2)."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_events_payload_gin",
            "events",
            ["payload"],
            postgresql_using="gin",
            postgresql_ops={"payload": "jsonb_path_ops"},
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_events_payload_gin",
            table_name="events",
            postgresql_concurrently=True,
        )
