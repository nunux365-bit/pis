"""O2C: MIS summary row employee fields + unique key fix

Revision ID: 010_o2c_mis_summary_row_employee_fields
Revises: 009_o2c_mis_drafts
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_o2c_mis_summary_row_employee_fields"
down_revision: Union[str, None] = "009_o2c_mis_drafts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("o2c_mis_summary_row", sa.Column("employee_external_id", sa.Text(), nullable=True))
    op.add_column("o2c_mis_summary_row", sa.Column("employee_name", sa.Text(), nullable=True))
    op.add_column("o2c_mis_summary_row", sa.Column("contracted_count", sa.Numeric(18, 4), nullable=True))
    op.add_column("o2c_mis_summary_row", sa.Column("absent_days", sa.Numeric(18, 4), nullable=True))
    op.add_column("o2c_mis_summary_row", sa.Column("comments", sa.Text(), nullable=True))

    # 009 created a uniqueness on (mis_run_id, contract_rate_line_id, description).
    # With “Service” being constant per contract_rate_line, description must no
    # longer be part of uniqueness. We key per employee row via employee_external_id.
    op.drop_index("uq_o2c_mis_summary_unique_line", table_name="o2c_mis_summary_row")
    op.create_index(
        "uq_o2c_mis_summary_employee",
        "o2c_mis_summary_row",
        ["mis_run_id", "contract_rate_line_id", "employee_external_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_o2c_mis_summary_employee", table_name="o2c_mis_summary_row")
    op.create_index(
        "uq_o2c_mis_summary_unique_line",
        "o2c_mis_summary_row",
        ["mis_run_id", "contract_rate_line_id", "description"],
        unique=True,
    )
    op.drop_column("o2c_mis_summary_row", "comments")
    op.drop_column("o2c_mis_summary_row", "absent_days")
    op.drop_column("o2c_mis_summary_row", "contracted_count")
    op.drop_column("o2c_mis_summary_row", "employee_name")
    op.drop_column("o2c_mis_summary_row", "employee_external_id")

