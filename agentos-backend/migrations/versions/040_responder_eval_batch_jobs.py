# migrations/versions/040_responder_eval_batch_jobs.py
"""responder_eval_batch_jobs for OpenAI Batch API judge runs."""

from alembic import op

revision = "040_responder_eval_batch_jobs"
down_revision = "039_responder_eval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS responder_eval_batch_jobs (
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
        CREATE INDEX IF NOT EXISTS ix_responder_eval_batch_jobs_status
        ON responder_eval_batch_jobs (status, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_batch_jobs_status;")
    op.execute("DROP TABLE IF EXISTS responder_eval_batch_jobs;")
