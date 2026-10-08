"""Cold outreach feature — leads, replies tables + catalog agent seed.

Merged from original 031_add_outreach_tables, 032_update_outreach_schema,
033_cold_outreach_feature into a single migration (net effect only).

Revision ID: 031_cold_outreach_feature
Revises: 030_email_automation_sends_business_key_idx
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "031_cold_outreach_feature"
down_revision: Union[str, None] = "030_email_automation_sends_business_key_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── outreach_leads ───────────────────────────────────────────────────────
    op.create_table(
        "outreach_leads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_name", sa.String(80), nullable=False),
        sa.Column("gsheet_row_index", sa.Integer(), nullable=False),
        sa.Column("sr_no", sa.Integer(), nullable=True),
        sa.Column("bd_lead_name", sa.String(256), nullable=True),
        sa.Column("account_name", sa.String(256), nullable=True),
        sa.Column("spoc", sa.String(256), nullable=True),
        sa.Column("designation", sa.String(256), nullable=True),
        sa.Column("email_id", sa.Text(), nullable=True),
        sa.Column("primary_email", sa.String(320), nullable=True),
        sa.Column("industry", sa.String(128), nullable=True),
        sa.Column("company_size", sa.Integer(), nullable=True),
        sa.Column("target_service_1", sa.String(128), nullable=True),
        sa.Column("target_service_2", sa.String(128), nullable=True),
        sa.Column("bd_lead_email", sa.Text(), nullable=True),
        sa.Column("bd_head_email", sa.Text(), nullable=True),
        sa.Column("tab_name", sa.String(128), nullable=True),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("subject_sent", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'hold'")),
        sa.Column("skipped_reason", sa.String(64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column("send_attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("gmail_thread_id", sa.String(128), nullable=True),
        sa.Column("gmail_message_id", sa.String(128), nullable=True),
        sa.Column("last_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reply_category", sa.String(64), nullable=True),
        sa.Column("reply_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("issues", JSONB(), nullable=False, server_default="[]"),
        sa.Column("issues_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gsheet_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gsheet_written_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_name", "primary_email", name="uq_outreach_leads_campaign_email"),
    )
    op.create_index("ix_outreach_leads_campaign_status", "outreach_leads", ["campaign_name", "status"])
    op.create_index("ix_outreach_leads_gmail_thread_id", "outreach_leads", ["gmail_thread_id"])

    # ── outreach_replies ─────────────────────────────────────────────────────
    op.create_table(
        "outreach_replies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("outreach_lead_id", sa.Integer(), nullable=False),
        sa.Column("campaign_name", sa.String(80), nullable=False),
        sa.Column("gmail_thread_id", sa.String(128), nullable=False),
        sa.Column("gmail_message_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("intent_level", sa.String(32), nullable=True),
        sa.Column("next_action", sa.String(64), nullable=True),
        sa.Column("key_signals", JSONB(), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["outreach_lead_id"], ["outreach_leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gmail_message_id", name="uq_outreach_replies_message_id"),
    )
    op.create_index("ix_outreach_replies_lead_id", "outreach_replies", ["outreach_lead_id"])
    op.create_index("ix_outreach_replies_thread_id", "outreach_replies", ["gmail_thread_id"])

    # ── Seed Cold Outreach Agent catalog entry ───────────────────────────────
    op.execute(
        sa.text(
            """
            INSERT INTO catalog_agents (id, slug, name, status, ring, description, tasks_today, uptime_pct, sort_order)
            VALUES (
                :id,
                'cold-outreach-agent',
                'Cold Outreach Agent',
                'active',
                2,
                'Weekly cold email dispatch across all active campaigns, with daily GSheet sync.',
                0,
                99.900,
                7
            )
            ON CONFLICT (slug) DO NOTHING
            """
        ).bindparams(id=str(uuid.uuid4()))
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM catalog_agents WHERE slug = 'cold-outreach-agent'"))
    op.drop_index("ix_outreach_replies_thread_id", table_name="outreach_replies")
    op.drop_index("ix_outreach_replies_lead_id", table_name="outreach_replies")
    op.drop_table("outreach_replies")
    op.drop_index("ix_outreach_leads_gmail_thread_id", table_name="outreach_leads")
    op.drop_index("ix_outreach_leads_campaign_status", table_name="outreach_leads")
    op.drop_table("outreach_leads")
