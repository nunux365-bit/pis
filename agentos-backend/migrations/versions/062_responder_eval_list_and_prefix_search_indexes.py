"""responder eval: list ORDER BY index + chat_id prefix search index."""

from alembic import op

revision = "062_responder_eval_list_and_prefix_search_indexes"
down_revision = "061_merge_prosight_responder_eval_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Default Eval runs list: filter eval_version + created_at window, ORDER BY created_at DESC, id DESC.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_list_created
        ON responder_eval_runs (eval_version, created_at DESC, id DESC);
        """
    )
    # Prefix search: chat_id LIKE 'prefix%' (numeric ids). order_id uses 059 text_pattern_ops index.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_chat_id_prefix
        ON responder_eval_runs (eval_version, chat_id text_pattern_ops);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_chat_id_prefix;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_list_created;")
