"""responder eval: denormalized segment columns + dashboard index."""

from alembic import op

revision = "050_responder_eval_segment_columns"
down_revision = "049_responder_eval_processing_reclaim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE responder_eval_runs
        ADD COLUMN IF NOT EXISTS bot_score INTEGER,
        ADD COLUMN IF NOT EXISTS human_score INTEGER,
        ADD COLUMN IF NOT EXISTS bot_grade VARCHAR(1) NOT NULL DEFAULT '-',
        ADD COLUMN IF NOT EXISTS human_grade VARCHAR(1) NOT NULL DEFAULT '-';
        """
    )
    op.execute(
        """
        UPDATE responder_eval_runs
        SET
            bot_score = CASE
                WHEN COALESCE((eval_json #>> '{segment_evals,bot,graded}')::boolean, false)
                THEN NULLIF(eval_json #>> '{segment_evals,bot,composite_score}', '')::integer
                ELSE NULL
            END,
            human_score = CASE
                WHEN COALESCE((eval_json #>> '{segment_evals,human_agent,graded}')::boolean, false)
                THEN NULLIF(eval_json #>> '{segment_evals,human_agent,composite_score}', '')::integer
                ELSE NULL
            END,
            bot_grade = COALESCE(NULLIF(eval_json #>> '{segment_evals,bot,letter_grade}', ''), '-'),
            human_grade = COALESCE(NULLIF(eval_json #>> '{segment_evals,human_agent,letter_grade}', ''), '-');
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_status_created;")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_dashboard
        ON responder_eval_runs (eval_version, eval_status, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_dashboard;")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_status_created
        ON responder_eval_runs (eval_status, created_at DESC);
        """
    )
    op.execute(
        """
        ALTER TABLE responder_eval_runs
        DROP COLUMN IF EXISTS bot_score,
        DROP COLUMN IF EXISTS human_score,
        DROP COLUMN IF EXISTS bot_grade,
        DROP COLUMN IF EXISTS human_grade;
        """
    )
