"""O2C: drop legacy UNIQUE on (mis_run_id, contract_rate_line_id, description)

DBs created from raw SQL (DDL_PATCH_O2C) used CREATE TABLE ... UNIQUE(...);
PostgreSQL names that constraint (e.g. ..._descri_key). Alembic 010 only dropped
``uq_o2c_mis_summary_unique_line``, so some databases still block multiple
employees per contract_rate_line_id. Per-employee uniqueness is
``uq_o2c_mis_summary_employee``.

Revision ID: 012_o2c_mis_summary_drop_legacy_description_unique
Revises: 011_o2c_mis_run_summary_json
Create Date: 2026-03-26
"""

from typing import Sequence, Union

from alembic import op

revision: str = "012_o2c_mis_summary_drop_legacy_description_unique"
down_revision: Union[str, None] = "011_o2c_mis_run_summary_json"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Auto-generated names from UNIQUE(...) in CREATE TABLE (63-char truncation variants).
    for cname in (
        "o2c_mis_summary_row_mis_run_id_contract_rate_line_id_descri_key",
        "o2c_mis_summary_row_mis_run_id_contract_rate_line_id_description_key",
    ):
        op.execute(
            f'ALTER TABLE o2c_mis_summary_row DROP CONSTRAINT IF EXISTS "{cname}"'
        )
    # If raw DDL used a named unique index instead of a table constraint, remove it.
    op.execute("DROP INDEX IF EXISTS uq_o2c_mis_summary_unique_line")


def downgrade() -> None:
    # Do not restore description-based uniqueness (breaks multi-employee MIS rows).
    pass
