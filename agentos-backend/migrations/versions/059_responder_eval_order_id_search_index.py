"""responder eval: btree index for dashboard order_id prefix search."""

from alembic import op

revision = "059_responder_eval_order_id_search_index"
down_revision = "058_responder_eval_chart_window_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dashboard list search: order_id ILIKE 'prefix%' (see list_responder_eval_runs).
    # eval_version is always filtered; text_pattern_ops supports prefix ILIKE/LIKE.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_order_id_prefix
        ON responder_eval_runs (eval_version, order_id text_pattern_ops)
        WHERE order_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_order_id_prefix;")
