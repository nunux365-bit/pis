"""Drop responder_eval_batch_jobs — sync judge only."""

from alembic import op

revision = "042_drop_responder_eval_batch_jobs"
down_revision = "041_responder_eval_dump_last_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE order_rca_eval_dumps
        SET eval_status = 'pending', last_error = NULL, updated_at = now()
        WHERE eval_status = 'batch_queued';
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_batch_jobs_status;")
    op.execute("DROP TABLE IF EXISTS responder_eval_batch_jobs;")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE responder_eval_batch_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            openai_batch_id TEXT NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL DEFAULT 'submitted',
            custom_id_map JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX ix_responder_eval_batch_jobs_status
        ON responder_eval_batch_jobs (status, created_at DESC);
        """
    )
