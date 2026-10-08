"""responder eval: index on responder_eval_runs (eval_status, created_at)."""

from alembic import op

revision = "048_responder_eval_runs_status_created_idx"
down_revision = "047_seed_routing_matrix_and_workflow_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_status_created
        ON responder_eval_runs (eval_status, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_status_created;")
