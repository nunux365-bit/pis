"""workflow_runs: mysql_call_id column for compliance_call MySQL ingest watermark + index.

Revision ID: 029_workflow_runs_mysql_call_id
Revises: 028_gmail_intelligence
"""

from typing import Sequence, Union

from alembic import op

revision: str = "029_workflow_runs_mysql_call_id"
down_revision: Union[str, None] = "028_gmail_intelligence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workflow_runs
        ADD COLUMN IF NOT EXISTS mysql_call_id BIGINT NULL;
        """
    )
    op.execute(
        """
        UPDATE workflow_runs SET mysql_call_id = (input_data->>'mysql_call_id')::bigint
        WHERE workflow_key = 'compliance_call'
          AND COALESCE(input_data->>'source', '') = 'mysql_call'
          AND input_data ? 'mysql_call_id'
          AND (input_data->>'mysql_call_id') ~ '^[0-9]+$';
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_workflow_runs_cc_mysql_completed_call_id
        ON workflow_runs (mysql_call_id DESC)
        WHERE workflow_key = 'compliance_call'
          AND status = 'completed'
          AND mysql_call_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_workflow_runs_cc_mysql_completed_call_id;")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS mysql_call_id;")
