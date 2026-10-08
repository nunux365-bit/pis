"""Hand-off classification columns on responder_eval_runs."""

from alembic import op

revision = "064_responder_eval_handoff_classification"
down_revision = "063_resource_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE responder_eval_runs
          ADD COLUMN IF NOT EXISTS handoff_bucket VARCHAR(64),
          ADD COLUMN IF NOT EXISTS handoff_sub_bucket VARCHAR(128);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_handoff_daily
        ON responder_eval_runs (eval_version, handoff_bucket, created_at DESC)
        WHERE human_score IS NOT NULL AND handoff_bucket IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_handoff_daily;")
    op.execute(
        """
        ALTER TABLE responder_eval_runs
          DROP COLUMN IF EXISTS handoff_sub_bucket,
          DROP COLUMN IF EXISTS handoff_bucket;
        """
    )
