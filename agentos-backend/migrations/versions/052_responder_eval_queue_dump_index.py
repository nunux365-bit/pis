"""responder eval: queue dump index for all scheduler backlog statuses."""

from alembic import op

revision = "052_responder_eval_queue_dump_index"
down_revision = "051_responder_eval_grade_filter_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_order_rca_eval_dumps_eval_queue
        ON order_rca_eval_dumps (created_at ASC, id ASC)
        WHERE eval_status IN (
            'pending', 'failed', 'waiting_chat', 'abandoned', 'processing'
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_order_rca_eval_dumps_eval_queue;")
