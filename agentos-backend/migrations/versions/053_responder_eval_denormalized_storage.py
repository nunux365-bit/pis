"""responder eval: denormalized resolutions, issue tags, split chat/ground-truth JSON."""

from alembic import op

revision = "053_responder_eval_denormalized_storage"
down_revision = "052_responder_eval_queue_dump_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE responder_eval_runs
        ADD COLUMN IF NOT EXISTS composite_resolution VARCHAR(16),
        ADD COLUMN IF NOT EXISTS bot_resolution VARCHAR(16),
        ADD COLUMN IF NOT EXISTS human_resolution VARCHAR(16),
        ADD COLUMN IF NOT EXISTS composite_issues TEXT[] NOT NULL DEFAULT '{}',
        ADD COLUMN IF NOT EXISTS bot_issues TEXT[] NOT NULL DEFAULT '{}',
        ADD COLUMN IF NOT EXISTS human_issues TEXT[] NOT NULL DEFAULT '{}',
        ADD COLUMN IF NOT EXISTS chat_json JSONB,
        ADD COLUMN IF NOT EXISTS ground_truth_json JSONB;
        """
    )
    op.execute(
        """
        UPDATE responder_eval_runs
        SET
            composite_resolution = eval_json->>'resolution',
            bot_resolution = eval_json #>> '{segment_evals,bot,resolution}',
            human_resolution = eval_json #>> '{segment_evals,human_agent,resolution}'
        WHERE composite_resolution IS NULL
          AND eval_json IS NOT NULL;
        """
    )
    op.execute(
        """
        UPDATE responder_eval_runs
        SET
            chat_json = eval_json->'chat',
            ground_truth_json = eval_json->'eval_ground_truth',
            eval_json = eval_json - 'chat' - 'eval_ground_truth'
        WHERE chat_json IS NULL
          AND (eval_json ? 'chat' OR eval_json ? 'eval_ground_truth');
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_composite_resolution
        ON responder_eval_runs (eval_version, eval_status, composite_resolution, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_responder_eval_runs_composite_issues
        ON responder_eval_runs USING GIN (composite_issues);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE responder_eval_runs
        SET eval_json = eval_json
            || CASE WHEN chat_json IS NOT NULL THEN jsonb_build_object('chat', chat_json) ELSE '{}'::jsonb END
            || CASE WHEN ground_truth_json IS NOT NULL
                THEN jsonb_build_object('eval_ground_truth', ground_truth_json) ELSE '{}'::jsonb END
        WHERE chat_json IS NOT NULL OR ground_truth_json IS NOT NULL;
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_composite_issues;")
    op.execute("DROP INDEX IF EXISTS ix_responder_eval_runs_composite_resolution;")
    op.execute(
        """
        ALTER TABLE responder_eval_runs
        DROP COLUMN IF EXISTS composite_resolution,
        DROP COLUMN IF EXISTS bot_resolution,
        DROP COLUMN IF EXISTS human_resolution,
        DROP COLUMN IF EXISTS composite_issues,
        DROP COLUMN IF EXISTS bot_issues,
        DROP COLUMN IF EXISTS human_issues,
        DROP COLUMN IF EXISTS chat_json,
        DROP COLUMN IF EXISTS ground_truth_json;
        """
    )
