"""O2C: o2c_attendance_site_recon (invoice skip queue; not site_alias — see module docstring).

Revision ID: 008_o2c_recon
Revises: 007_o2c
Create Date: 2026-03-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "008_o2c_recon"
down_revision: Union[str, None] = "007_o2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "o2c_attendance_site_recon",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_site_key", sa.Text(), nullable=False),
        sa.Column("billing_period_start", sa.Date(), nullable=False),
        sa.Column("billing_period_end", sa.Date(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("attendance_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "llm_match_attempted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("resolved_service_site_id", UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["resolved_service_site_id"],
            ["service_site.id"],
            name="fk_o2c_attendance_site_recon_resolved_site",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "uq_o2c_attendance_site_recon_key",
        "o2c_attendance_site_recon",
        ["client_site_key", "billing_period_start", "billing_period_end", "reason_code"],
        unique=True,
    )
    op.create_index(
        "ix_o2c_attendance_site_recon_open",
        "o2c_attendance_site_recon",
        ["status", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_o2c_attendance_site_recon_open", table_name="o2c_attendance_site_recon")
    op.drop_index("uq_o2c_attendance_site_recon_key", table_name="o2c_attendance_site_recon")
    op.drop_table("o2c_attendance_site_recon")
