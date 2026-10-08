"""workflow_runs: ingest_fingerprint for compliance_call dedupe + index."""

from alembic import op

revision = "024_workflow_runs_ingest_fingerprint"
down_revision = "023_ea_send_rfc_message_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workflow_runs
        ADD COLUMN IF NOT EXISTS ingest_fingerprint VARCHAR(128) NULL;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_runs_key_ingest_fingerprint
        ON workflow_runs (workflow_key, ingest_fingerprint)
        WHERE ingest_fingerprint IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_workflow_runs_workflow_key_created_at
        ON workflow_runs (workflow_key, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_workflow_runs_workflow_key_created_at;")
    op.execute("DROP INDEX IF EXISTS uq_workflow_runs_key_ingest_fingerprint;")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS ingest_fingerprint;")
