"""Create INDEX CONCURRENTLY on messages(session_id, seq) (§6.1).

The chat timeline sorts by ``(session_id, seq)`` so the renderer can stream a
session's messages in insertion order without an extra sort step. Mirrors the
concurrent-index pattern used by every other §6.1 index migration
(``0022`` onwards).
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_session_seq",
            "messages",
            ["session_id", "seq"],
            unique=False,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_messages_session_seq",
            table_name="messages",
            postgresql_concurrently=True,
        )
