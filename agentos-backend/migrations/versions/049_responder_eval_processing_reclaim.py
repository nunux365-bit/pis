"""responder eval: stale processing reclaim index."""

from alembic import op

revision = "049_responder_eval_processing_reclaim"
down_revision = "048_responder_eval_runs_status_created_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_order_rca_eval_dumps_stale_processing
        ON order_rca_eval_dumps (updated_at, id)
        WHERE eval_status = 'processing';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_order_rca_eval_dumps_stale_processing;")
