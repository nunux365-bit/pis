"""add composite index on email_automation_sends(business_key, status, sent_at)

Revision ID: 030_email_automation_sends_business_key_idx
Revises: 029_workflow_runs_mysql_call_id
Create Date: 2026-05-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030_email_automation_sends_business_key_idx"
down_revision: Union[str, None] = "029_workflow_runs_mysql_call_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes("email_automation_sends")}
    if "ix_email_automation_sends_business_key_status_sent_at" not in existing:
        op.create_index(
            "ix_email_automation_sends_business_key_status_sent_at",
            "email_automation_sends",
            ["business_key", "status", "sent_at"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_email_automation_sends_business_key_status_sent_at",
        table_name="email_automation_sends",
    )
