# migrations/versions/038_service_site_site_key.py
"""service_site: add site_key column

Revision ID: 038_service_site_site_key
Revises: 037_sbi_runs_raw_metrics_flag
Create Date: 2026-07-09

Adds the nullable ``site_key`` text column to ``service_site``.  The reference
schema (schema_backup.sql) and the application SQL both assume this column
exists: ingest INSERTs into ``service_site (... site_key)`` and read queries do
``COALESCE(ss.site_key, ss.canonical_name, ...)``.  Databases whose service_site
table was created from the ORM model (which omitted the column) were missing it,
causing ``UndefinedColumnError: column ss.site_key does not exist`` on MIS uploads.
"""
from __future__ import annotations
from typing import Union
from alembic import op
import sqlalchemy as sa

revision: str = "038_service_site_site_key"
down_revision: Union[str, None] = "037_sbi_runs_raw_metrics_flag"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name: str) -> bool:
    insp = sa.inspect(bind)
    return insp.has_table(table_name)


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    insp = sa.inspect(bind)
    return column_name in {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "service_site") and not _column_exists(
        bind, "service_site", "site_key"
    ):
        op.add_column(
            "service_site",
            sa.Column("site_key", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "service_site") and _column_exists(
        bind, "service_site", "site_key"
    ):
        op.drop_column("service_site", "site_key")
