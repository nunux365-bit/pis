"""procurement_tickets + pr_po_reference_values (PR/PO automation).

Revision ID: 016_procurement_pr_po
Revises: 015_contract_rate_line_is_active
Create Date: 2026-04-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "016_procurement_pr_po"
down_revision: Union[str, None] = "015_contract_rate_line_is_active"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "procurement_tickets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("parent_pr_id", UUID(as_uuid=True), nullable=True),
        sa.Column("document_type", sa.String(8), nullable=False),
        sa.Column("form", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attachments", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("drive_folder_id", sa.String(128), nullable=True),
        sa.Column("sap_id", sa.String(64), nullable=True),
        sa.Column(
            "sap_sync",
            JSONB,
            nullable=False,
            server_default=sa.text(
                '\'{"attempt_count": 0, "next_retry_at": null, "last_error": null}\'::jsonb'
            ),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["parent_pr_id"], ["procurement_tickets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_procurement_tickets_user_created", "procurement_tickets", ["created_by_user_id", "created_at"])
    op.create_index("ix_procurement_tickets_parent_pr", "procurement_tickets", ["parent_pr_id"])
    op.create_index("ix_procurement_tickets_kind", "procurement_tickets", ["kind"])

    op.create_table(
        "pr_po_reference_values",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("domain", sa.String(80), nullable=False),
        sa.Column("document_type", sa.String(8), nullable=False, server_default=""),
        sa.Column("code", sa.String(120), nullable=False),
        sa.Column("label", sa.String(500), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("domain", "document_type", "code", name="uq_pr_po_ref_domain_type_code"),
    )
    op.create_index("ix_pr_po_ref_domain_type", "pr_po_reference_values", ["domain", "document_type"])


def downgrade() -> None:
    op.drop_index("ix_pr_po_ref_domain_type", table_name="pr_po_reference_values")
    op.drop_table("pr_po_reference_values")
    op.drop_index("ix_procurement_tickets_kind", table_name="procurement_tickets")
    op.drop_index("ix_procurement_tickets_parent_pr", table_name="procurement_tickets")
    op.drop_index("ix_procurement_tickets_user_created", table_name="procurement_tickets")
    op.drop_table("procurement_tickets")
