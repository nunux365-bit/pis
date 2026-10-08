"""B-tree index on vendor rows (code) for prefix / numeric search.

Revision ID: 018_pr_po_ref_vendor_code_index
Revises: 017_pr_po_reference_applies_to_kind
Create Date: 2026-04-14

``pg_trgm`` is not required: ~200k vendors with ``company_code`` suffix filter and
prefix search on ``split_part(code, '|', 1)`` stay fast enough for interactive UI.
This partial index helps ``code LIKE '100%'``-style plans when the planner uses it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018_pr_po_ref_vendor_code_index"
down_revision: Union[str, None] = "017_pr_po_reference_applies_to_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_pr_po_ref_vendor_code",
        "pr_po_reference_values",
        ["code"],
        unique=False,
        postgresql_where=sa.text("domain = 'vendor'"),
    )


def downgrade() -> None:
    op.drop_index("ix_pr_po_ref_vendor_code", table_name="pr_po_reference_values")
