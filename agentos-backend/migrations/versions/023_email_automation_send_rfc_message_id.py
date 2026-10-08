"""Email automation: outbound RFC Message-ID for weekly thread chaining.

Revision ID: 023_ea_send_rfc_message_id
Revises: 022_receivable_dashboard_snapshots
Create Date: 2026-05-01

``provider_rfc_message_id`` stores the ``Message-ID`` header of the message
Gmail accepted for this row (fetched via metadata API after send). The next
period's reminder for the same (workflow_type, variant, business_key) uses it
as ``In-Reply-To`` / ``References`` so recurring reminders stay in one thread.

Partial index speeds ``ORDER BY sent_at DESC LIMIT 1`` for prior-send lookup.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023_ea_send_rfc_message_id"
down_revision: Union[str, None] = "022_receivable_dashboard_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_automation_sends",
        sa.Column("provider_rfc_message_id", sa.String(length=512), nullable=True),
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX ix_email_automation_sends_thread_prior_lookup
            ON email_automation_sends (
                workflow_type,
                variant,
                business_key,
                sent_at DESC
            )
            WHERE status = 'sent'
              AND provider_rfc_message_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP INDEX IF EXISTS ix_email_automation_sends_thread_prior_lookup")
    )
    op.drop_column("email_automation_sends", "provider_rfc_message_id")
