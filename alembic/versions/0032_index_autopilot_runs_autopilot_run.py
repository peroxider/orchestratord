"""Create INDEX CONCURRENTLY on autopilot_runs(autopilot_id, run_id) (§6.1)."""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_autopilot_runs_autopilot_run",
            "autopilot_runs",
            ["autopilot_id", "run_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_autopilot_runs_autopilot_run",
            table_name="autopilot_runs",
            postgresql_concurrently=True,
        )
