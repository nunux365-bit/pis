"""compliance_call_runs: projection table for compliance_call dashboard (source_file_id + source_type)."""

from alembic import op

revision = "026_compliance_call_runs"
down_revision = "025_alembic_version_num_length"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE compliance_call_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow_run_id UUID NOT NULL
                REFERENCES workflow_runs(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(32) NOT NULL,
            filename TEXT,
            doctor_slug TEXT,
            doctor_name TEXT,
            source_file_id TEXT,
            source_type VARCHAR(32) NOT NULL DEFAULT 'drive',
            composite_pct NUMERIC(12, 4),
            grade VARCHAR(64),
            grade_label TEXT,
            sheet_appended BOOLEAN,
            error_message TEXT,
            ingest_fingerprint VARCHAR(128)
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_compliance_call_runs_workflow_run_id
        ON compliance_call_runs (workflow_run_id);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_compliance_call_runs_created_at
        ON compliance_call_runs (created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_compliance_call_runs_status_created
        ON compliance_call_runs (status, created_at DESC);
        """
    )
    op.execute(
        """
        INSERT INTO compliance_call_runs (
            id, workflow_run_id, created_at, updated_at, status,
            filename, doctor_slug, doctor_name, source_file_id, source_type,
            composite_pct, grade, grade_label, sheet_appended, error_message, ingest_fingerprint
        )
        SELECT
            gen_random_uuid(),
            wr.id,
            wr.created_at,
            wr.updated_at,
            wr.status,
            wr.input_data->>'filename',
            wr.input_data->>'doctor_slug',
            wr.input_data->>'doctor_name',
            NULLIF(TRIM(COALESCE(wr.input_data->>'source_file_id', wr.input_data->>'drive_file_id')), ''),
            CASE
                WHEN lower(trim(coalesce(wr.input_data->>'source_type', ''))) IN ('drive', 'aws', 'ozontel')
                    THEN lower(trim(wr.input_data->>'source_type'))
                WHEN lower(trim(coalesce(wr.input_data->>'source', ''))) IN ('gdrive', 'google_drive', 'drive')
                    THEN 'drive'
                WHEN lower(trim(coalesce(wr.input_data->>'source', ''))) = 'aws'
                    THEN 'aws'
                WHEN lower(trim(coalesce(wr.input_data->>'source', ''))) = 'ozontel'
                    THEN 'ozontel'
                ELSE 'drive'
            END,
            CASE
                WHEN (wr.output_data->'eval'->>'composite_pct') ~ '^[0-9]+(\\.[0-9]*)?$'
                THEN (wr.output_data->'eval'->>'composite_pct')::numeric
                ELSE NULL
            END,
            NULLIF(trim(wr.output_data->'eval'->>'grade'), ''),
            NULLIF(trim(wr.output_data->'eval'->>'grade_label'), ''),
            CASE lower(coalesce(wr.output_data->>'sheet_appended', ''))
                WHEN 'true' THEN true
                WHEN 'false' THEN false
                ELSE NULL
            END,
            wr.error_message,
            wr.ingest_fingerprint
        FROM workflow_runs wr
        WHERE wr.workflow_key = 'compliance_call'
          AND NOT EXISTS (
              SELECT 1 FROM compliance_call_runs c WHERE c.workflow_run_id = wr.id
          );
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_compliance_call_runs_status_created;")
    op.execute("DROP INDEX IF EXISTS ix_compliance_call_runs_created_at;")
    op.execute("DROP INDEX IF EXISTS uq_compliance_call_runs_workflow_run_id;")
    op.execute("DROP TABLE IF EXISTS compliance_call_runs;")
