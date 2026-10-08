"""O2C: store LLM MIS Summary JSON on mis_run

Revision ID: 011_o2c_mis_run_summary_json
Revises: 010_o2c_mis_summary_row_employee_fields
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "011_o2c_mis_run_summary_json"
down_revision: Union[str, None] = "010_o2c_mis_summary_row_employee_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("o2c_mis_run", sa.Column("summary_json", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("o2c_mis_run", "summary_json")

