"""Receivable executive dashboard snapshots (weekly .xlsx payload JSON).

Revision ID: 022_receivable_dashboard_snapshots
Revises: 021_pr_po_ref_vendor_trgm
Create Date: 2026-04-23

One table:

* ``receivable_dashboard_snapshots`` — append-only rows from ingesting the
  receivables workbook after payment-reminder attachment download. ``payload``
  is the denormalized dashboard JSON (KPIs, grids, unbilled, top parties).
  Optional ``source_message_id`` links to the inbound
  ``email_automation_messages`` row for traceability.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "022_receivable_dashboard_snapshots"
down_revision: Union[str, None] = "021_pr_po_ref_vendor_trgm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "receivable_dashboard_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_automation_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_receivable_dashboard_snapshots_created_at",
        "receivable_dashboard_snapshots",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receivable_dashboard_snapshots_created_at",
        table_name="receivable_dashboard_snapshots",
    )
    op.drop_table("receivable_dashboard_snapshots")
