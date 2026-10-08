"""Add billing_profile to contract_terms_version (tcs | taco | generic).

Revision ID: 014_contract_terms_version_billing_profile
Revises: 013_o2c_mis_summary_row_human_correction
Create Date: 2026-03-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_contract_terms_version_billing_profile"
down_revision: Union[str, None] = "013_o2c_mis_summary_row_human_correction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contract_terms_version",
        sa.Column(
            "billing_profile",
            sa.Text(),
            nullable=False,
            server_default="generic",
        ),
    )


def downgrade() -> None:
    op.drop_column("contract_terms_version", "billing_profile")
