"""responder eval: indexes for bot cohorts + issue GIN parity."""

from alembic import op

revision = "056_responder_eval_bot_cohort_indexes"
down_revision = "055_sync_payroll_matrix_user_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial indexes: cohort + grade list filters for Bot closed / pre–hand-off views.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_bot_closed
        ON responder_eval_runs (eval_version, bot_grade, created_at DESC)
        WHERE bot_score IS NOT NULL AND human_score IS NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_bot_pre_handoff
        ON responder_eval_runs (eval_version, bot_grade, created_at DESC)
        WHERE bot_score IS NOT NULL AND human_score IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_bot_score_window
        ON responder_eval_runs (eval_version, eval_status, created_at DESC)
        WHERE bot_score IS NOT NULL;
        """
    )
    # GIN parity with composite_issues (prep for reason filters; cheap for cohort scans).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_bot_issues
        ON responder_eval_runs USING GIN (bot_issues);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_human_issues
        ON responder_eval_runs USING GIN (human_issues);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_human_issues;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_bot_issues;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_bot_score_window;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_bot_pre_handoff;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_bot_closed;")
