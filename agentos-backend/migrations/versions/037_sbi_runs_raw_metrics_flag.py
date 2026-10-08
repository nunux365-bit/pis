# migrations/versions/037_sbi_runs_raw_metrics_flag.py
"""sbi_runs: add raw_metrics_from_direct_read flag

Revision ID: 037_sbi_runs_raw_metrics_flag
Revises: 036_add_sbi_mis_tables
Create Date: 2026-06-09

Adds a boolean flag to sbi_runs that is set true only when raw_gmv_mrp and
raw_unique_order_ids were computed by reading the raw file directly (bypassing
load_raw() normalisation).  Previous uploads stored 0 for both fields due to a
column-naming mismatch (load_raw renames columns to letters; the upload code used
the original string names).  Recon uses this flag to skip the expensive disk re-read
when correct pre-computed values are already in the DB.
"""
from __future__ import annotations
from typing import Union
from alembic import op
import sqlalchemy as sa

revision: str = "037_sbi_runs_raw_metrics_flag"
down_revision: Union[str, None] = "036_add_sbi_mis_tables"
branch_labels = None
depends_on = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    insp = sa.inspect(bind)
    return column_name in {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _column_exists(bind, "sbi_runs", "raw_metrics_from_direct_read"):
        op.add_column(
            "sbi_runs",
            sa.Column(
                "raw_metrics_from_direct_read",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _column_exists(bind, "sbi_runs", "raw_metrics_from_direct_read"):
        op.drop_column("sbi_runs", "raw_metrics_from_direct_read")
