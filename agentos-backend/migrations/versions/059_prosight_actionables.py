"""Prosight actionables table for Databricks-synced action recommendations + feedback.

Revision ID: 059_prosight_actionables
Revises: 058_responder_eval_chart_window_index
Create Date: 2026-08-06

Renumbered from 057 when rebasing onto main @ c0e30d5: upstream added
057_responder_eval_reason_filter_indexes and 058_responder_eval_chart_window_index,
both descending from 056. Keeping this at 057/revising 056 would leave two heads
and break `alembic upgrade head`, so it now follows upstream's 058.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "059_prosight_actionables"
down_revision: Union[str, None] = "058_responder_eval_chart_window_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table_name: str) -> bool:
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "prosight_actionables"):
        op.create_table(
            "prosight_actionables",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("as_of_date", sa.Date(), nullable=True),
            sa.Column("bu", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("segment", sa.String(length=255), nullable=False),
            sa.Column("lens", sa.String(length=255), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=True),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("action_id", sa.Text(), nullable=True),
            sa.Column("action_hash", sa.String(length=64), nullable=False),
            sa.Column("is_marker", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "synced_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("model_version", sa.String(length=50), nullable=True),
            sa.Column("segment_label", sa.String(length=255), nullable=True),
            sa.Column("dimension", sa.String(length=64), nullable=True),
            sa.Column("impact_display", sa.String(length=255), nullable=True),
            sa.Column("fact", sa.Text(), nullable=True),
            sa.Column("l2_pocket", sa.Text(), nullable=True),
            sa.Column("why", sa.Text(), nullable=True),
            sa.Column("lever", sa.Text(), nullable=True),
            sa.Column("news_summary", sa.Text(), nullable=True),
            sa.Column("news_source", sa.String(length=255), nullable=True),
            sa.Column("news_url", sa.Text(), nullable=True),
            sa.Column("news_relation", sa.Text(), nullable=True),
            sa.Column("day_summary", sa.Text(), nullable=True),
            sa.Column("run_days", sa.Integer(), nullable=True),
            sa.Column("wow_pct", sa.Float(), nullable=True),
            sa.Column("dod_pct", sa.Float(), nullable=True),
            sa.Column("daily_order_gap", sa.Float(), nullable=True),
            sa.Column("l2_gap_orders", sa.Float(), nullable=True),
            sa.Column("l2_share_pct", sa.Float(), nullable=True),
            sa.Column("impact_inr_1d", sa.Float(), nullable=True),
            sa.Column("impact_inr_3d", sa.Float(), nullable=True),
            sa.Column("is_actionable", sa.Boolean(), nullable=True),
            sa.Column("days_saved", sa.Float(), nullable=True),
            sa.Column("feedback_updated_by", sa.String(length=255), nullable=True),
            sa.Column("feedback_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("action_hash", name="uq_prosight_actionables_hash"),
        )
        op.create_index(
            "ix_prosight_actionables_current_bu_date",
            "prosight_actionables",
            ["is_current", "bu", "as_of_date"],
            postgresql_using="btree",
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "prosight_actionables"):
        op.drop_index("ix_prosight_actionables_current_bu_date", table_name="prosight_actionables")
        op.drop_table("prosight_actionables")
