"""UNIQUE(session_id, seq) on messages (§6.1a seq-race fix).

The repository now locks the parent session row before assigning
``MAX(seq)+1``; this constraint is the backend guarantee that the
``after_seq`` tail-fetch contract can never be silently corrupted by a
residual concurrent-append path. Any rows duplicated by the pre-fix race
are deduplicated first (keeping the lexicographically first id per slot)
so the constraint installs cleanly on databases that ran the old code.
"""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM messages a
        USING messages b
        WHERE a.session_id = b.session_id
          AND a.seq = b.seq
          AND a.id > b.id
        """
    )
    op.create_unique_constraint(
        "uq_messages_session_seq", "messages", ["session_id", "seq"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_messages_session_seq", "messages")
