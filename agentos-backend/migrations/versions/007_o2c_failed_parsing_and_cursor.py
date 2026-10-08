"""O2C_OHC: failed_contract_parsing + o2c_ingestion_state (watermark per folder root).

Revision ID: 007_o2c
Revises: 006_session_run
Create Date: 2026-03-22

Requires agenos billing tables (billing_client, etc.) to already exist from agenos_setup.sql.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "007_o2c"
down_revision: Union[str, None] = "006_session_run"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "failed_contract_parsing",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_root", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("fs_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("validation_errors", JSONB, nullable=True),
        sa.Column("last_json_attempt", JSONB, nullable=True),
        sa.Column("markdown_excerpt", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_failed_contract_parsing_root_path",
        "failed_contract_parsing",
        ["source_root", "relative_path"],
    )
    op.create_index("ix_failed_contract_parsing_sha", "failed_contract_parsing", ["sha256"])

    op.create_table(
        "o2c_ingestion_state",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_root", sa.Text(), nullable=False, unique=True),
        sa.Column("last_processed_max_mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("o2c_ingestion_state")
    op.drop_index("ix_failed_contract_parsing_sha", table_name="failed_contract_parsing")
    op.drop_index("ix_failed_contract_parsing_root_path", table_name="failed_contract_parsing")
    op.drop_table("failed_contract_parsing")
