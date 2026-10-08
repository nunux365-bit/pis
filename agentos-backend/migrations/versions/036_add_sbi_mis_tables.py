# migrations/versions/036_add_sbi_mis_tables.py
"""add sbi mis tables

Revision ID: 036_add_sbi_mis_tables
Revises: 035_optimus_llm_usage
Create Date: 2026-06-05

"""
from __future__ import annotations
from typing import Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "036_add_sbi_mis_tables"
down_revision: Union[str, None] = "035_optimus_llm_usage"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name: str) -> bool:
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "sbi_rules"):
        op.create_table(
            "sbi_rules",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("sheet", sa.String(128), nullable=False),
            sa.Column("column_letter", sa.String(32), nullable=False),
            sa.Column("rule_type", sa.String(32), nullable=False),
            sa.Column("config_json", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("sheet", "column_letter", name="uq_sbi_rules_sheet_col"),
        )

    if not _table_exists(bind, "sbi_lookup_tables"):
        op.create_table(
            "sbi_lookup_tables",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("data_json", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_sbi_lookup_name"),
        )

    if not _table_exists(bind, "sbi_current_run"):
        op.create_table(
            "sbi_current_run",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("raw_file_path", sa.Text(), nullable=True),
            sa.Column("month", sa.String(7), nullable=True),
            sa.Column("row_count", sa.Integer(), nullable=True),
            sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint("id = 1", name="ck_sbi_current_run_singleton"),
        )

    if not _table_exists(bind, "sbi_runs"):
        op.create_table(
            "sbi_runs",
            sa.Column("month", sa.String(7), nullable=False),
            sa.Column("raw_file_path", sa.Text(), nullable=False),
            sa.Column("row_count", sa.Integer(), nullable=True),
            sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("raw_gmv_mrp", sa.Float(), nullable=True),
            sa.Column("raw_unique_order_ids", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("month"),
        )

    if not _table_exists(bind, "sbi_run_files"):
        op.create_table(
            "sbi_run_files",
            sa.Column("month", sa.String(7), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("original_filename", sa.String(256), nullable=True),
            sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("row_count", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("month", "kind"),
        )

    if not _table_exists(bind, "sbi_column_formats"):
        op.create_table(
            "sbi_column_formats",
            sa.Column("sheet", sa.String(128), nullable=False),
            sa.Column("column_letter", sa.String(32), nullable=False),
            sa.Column("number_format", sa.String(64), nullable=False),
            sa.PrimaryKeyConstraint("sheet", "column_letter"),
        )

    if not _table_exists(bind, "sbi_clients"):
        op.create_table(
            "sbi_clients",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("code", sa.String(32), nullable=False),
            sa.Column("display_name", sa.String(256), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code", name="uq_sbi_client_code"),
        )

    if not _table_exists(bind, "sbi_pf_historicals"):
        op.create_table(
            "sbi_pf_historicals",
            sa.Column("pf", sa.String(64), nullable=False),
            sa.Column("month", sa.String(7), nullable=False),
            sa.Column("pharma_total", sa.Float(), nullable=True),
            sa.Column("ahc_total", sa.Float(), nullable=True),
            sa.Column("pf_type", sa.String(32), nullable=True),
            sa.Column("wallet_limit", sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint("pf", "month"),
        )

    if not _table_exists(bind, "sbi_sheet_columns"):
        op.create_table(
            "sbi_sheet_columns",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("sheet", sa.String(128), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("col_letter", sa.String(32), nullable=False),
            sa.Column("header", sa.String(256), nullable=False),
            sa.Column("number_format", sa.String(64), nullable=True),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("sheet", "col_letter", name="uq_sbi_sheet_col_letter"),
            sa.UniqueConstraint("sheet", "position", name="uq_sbi_sheet_col_position"),
        )

    if not _table_exists(bind, "sbi_jobs"):
        op.create_table(
            "sbi_jobs",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("stage", sa.String(128), nullable=True),
            sa.Column("progress", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("result_json", postgresql.JSONB(), nullable=True),
            sa.Column("error_detail", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    bind = op.get_bind()

    for table in (
        "sbi_jobs", "sbi_sheet_columns", "sbi_pf_historicals",
        "sbi_clients", "sbi_column_formats", "sbi_run_files",
        "sbi_runs", "sbi_current_run", "sbi_lookup_tables", "sbi_rules",
    ):
        if _table_exists(bind, table):
            op.drop_table(table)
