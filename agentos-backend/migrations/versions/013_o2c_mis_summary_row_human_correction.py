"""O2C: add human_correction JSONB to o2c_mis_summary_row

Stores per-row diffs when a human corrects MIS values via inline edit.
Used as structured feedback for LLM in future MIS generation.

Revision ID: 013_o2c_mis_summary_row_human_correction
Revises: 012_o2c_mis_summary_drop_legacy_description_unique
Create Date: 2026-03-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "013_o2c_mis_summary_row_human_correction"
down_revision: Union[str, None] = "012_o2c_mis_summary_drop_legacy_description_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "o2c_mis_summary_row",
        sa.Column("human_correction", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("o2c_mis_summary_row", "human_correction")
