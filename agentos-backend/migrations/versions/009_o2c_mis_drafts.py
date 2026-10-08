"""O2C: MIS draft + approval tables (MIS-first; invoices created on MIS approval).

Revision ID: 009_o2c_mis_drafts
Revises: 008_o2c_recon
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "009_o2c_mis_drafts"
down_revision: Union[str, None] = "008_o2c_recon"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "o2c_mis_run",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("billing_client_id", UUID(as_uuid=True), nullable=False),
        sa.Column("service_site_id", UUID(as_uuid=True), nullable=False),
        sa.Column("contract_terms_version_id", UUID(as_uuid=True), nullable=False),
        sa.Column("client_site_key", sa.Text(), nullable=False),
        sa.Column("billing_period_start", sa.Date(), nullable=False),
        sa.Column("billing_period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending_human'")),
        sa.Column("template_path", sa.Text(), nullable=True),
        sa.Column("xlsx_path", sa.Text(), nullable=True),
        sa.Column("detailed_json", JSONB, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.Text(), nullable=True),
        sa.Column("rejection_notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["billing_client_id"], ["billing_client.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_site_id"], ["service_site.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["contract_terms_version_id"], ["contract_terms_version.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "uq_o2c_mis_run_site_period",
        "o2c_mis_run",
        ["service_site_id", "billing_period_start", "billing_period_end"],
        unique=True,
    )
    op.create_index(
        "ix_o2c_mis_run_status",
        "o2c_mis_run",
        ["status", "updated_at"],
    )

    op.create_table(
        "o2c_mis_summary_row",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("mis_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("contract_rate_line_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role_code", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("contractual_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("total_days", sa.Numeric(18, 4), nullable=True),
        sa.Column("attendance_days", sa.Numeric(18, 4), nullable=True),
        sa.Column("final_amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("is_omitted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("omit_reason", sa.Text(), nullable=True),
        sa.Column("calc_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["mis_run_id"], ["o2c_mis_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contract_rate_line_id"], ["contract_rate_line.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_o2c_mis_summary_run", "o2c_mis_summary_row", ["mis_run_id"])
    op.create_index(
        "uq_o2c_mis_summary_unique_line",
        "o2c_mis_summary_row",
        ["mis_run_id", "contract_rate_line_id", "description"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_o2c_mis_summary_unique_line", table_name="o2c_mis_summary_row")
    op.drop_index("ix_o2c_mis_summary_run", table_name="o2c_mis_summary_row")
    op.drop_table("o2c_mis_summary_row")
    op.drop_index("ix_o2c_mis_run_status", table_name="o2c_mis_run")
    op.drop_index("uq_o2c_mis_run_site_period", table_name="o2c_mis_run")
    op.drop_table("o2c_mis_run")

