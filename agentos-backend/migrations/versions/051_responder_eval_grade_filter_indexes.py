"""responder eval: indexes for segment grade + date list filters."""

from alembic import op

revision = "051_responder_eval_grade_filter_indexes"
down_revision = "050_responder_eval_segment_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_composite_grade
        ON responder_eval_runs (eval_version, letter_grade, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_bot_grade
        ON responder_eval_runs (eval_version, bot_grade, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_human_grade
        ON responder_eval_runs (eval_version, human_grade, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_human_grade;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_bot_grade;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_composite_grade;")
