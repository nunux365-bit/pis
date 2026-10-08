"""responder eval hardening: nullable composite_score, dump retry_count."""

from alembic import op

revision = "043_responder_eval_hardening"
down_revision = "042_drop_responder_eval_batch_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE responder_eval_runs
        ALTER COLUMN composite_score DROP NOT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE order_rca_eval_dumps
        ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_order_rca_eval_dumps_eval_pending;")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_order_rca_eval_dumps_eval_pending
        ON order_rca_eval_dumps (created_at, id)
        WHERE eval_status IN ('pending', 'failed', 'waiting_chat');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_order_rca_eval_dumps_eval_pending;")
    op.execute(
        """
        CREATE INDEX ix_order_rca_eval_dumps_eval_pending
        ON order_rca_eval_dumps (created_at, id)
        WHERE eval_status IN ('pending', 'failed');
        """
    )
    op.execute(
        """
        ALTER TABLE order_rca_eval_dumps
        DROP COLUMN IF EXISTS retry_count;
        """
    )
    op.execute(
        """
        UPDATE responder_eval_runs
        SET composite_score = 0
        WHERE composite_score IS NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE responder_eval_runs
        ALTER COLUMN composite_score SET NOT NULL;
        """
    )
