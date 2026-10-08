"""responder eval: order_rca_eval_dumps + responder_eval_runs."""

from alembic import op

revision = "039_responder_eval"
down_revision = "038_service_site_site_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS order_rca_eval_dumps (
            id SERIAL PRIMARY KEY,
            chat_id TEXT NOT NULL,
            response JSONB NOT NULL,
            eval_artifact JSONB NOT NULL,
            order_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            eval_status VARCHAR(16) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_order_rca_eval_dumps_chat_id
        ON order_rca_eval_dumps (chat_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_order_rca_eval_dumps_eval_pending
        ON order_rca_eval_dumps (created_at, id)
        WHERE eval_status IN ('pending', 'failed');
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS responder_eval_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chat_id TEXT NOT NULL,
            order_id TEXT,
            rca_run_id TEXT,
            eval_version TEXT NOT NULL,
            composite_score INTEGER NOT NULL,
            letter_grade VARCHAR(1) NOT NULL,
            eval_status VARCHAR(16) NOT NULL DEFAULT 'completed',
            eval_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_responder_eval_runs_chat_id
        ON responder_eval_runs (chat_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_grade_created
        ON responder_eval_runs (letter_grade, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_grade_created;")
    op.execute("DROP INDEX IF EXISTS uq_responder_eval_runs_chat_id;")
    op.execute("DROP TABLE IF EXISTS responder_eval_runs;")
    op.execute("DROP INDEX IF EXISTS ix_order_rca_eval_dumps_eval_pending;")
    op.execute("DROP INDEX IF EXISTS uq_order_rca_eval_dumps_chat_id;")
    op.execute("DROP TABLE IF EXISTS order_rca_eval_dumps;")
