"""Email automation: send_attempt_count column + composite reclaim index.

Revision ID: 020_ea_send_attempts
Revises: 019_email_automation
Create Date: 2026-04-16

Two small but important additions on top of ``email_automation_sends``:

1. ``send_attempt_count INT NOT NULL DEFAULT 0`` — dispatch increments this
   inside the ``SELECT … FOR UPDATE SKIP LOCKED`` claim so it counts exactly
   once per attempt. The retry endpoint refuses auto-retry when it reaches
   ``settings.email_automation_send_max_attempts`` (default 5). Without a
   cap a single poison row can pin a dispatcher forever on every tick.
2. ``ix_email_automation_sends_status_updated (status, updated_at)`` — the
   reclaim sweep (``status='sending' AND updated_at < cutoff``) was doing a
   bitmap scan under the single-column ``status`` index. Composite index
   turns it into a bounded index range scan; important once the table
   holds months of sent/failed rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "020_ea_send_attempts"
down_revision: Union[str, None] = "019_email_automation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default kept for the backfill; the ORM column default handles
    # every new insert going forward so the server_default can be dropped in
    # a later migration if desired.
    op.add_column(
        "email_automation_sends",
        sa.Column(
            "send_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_index(
        "ix_email_automation_sends_status_updated",
        "email_automation_sends",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_automation_sends_status_updated",
        table_name="email_automation_sends",
    )
    op.drop_column("email_automation_sends", "send_attempt_count")
