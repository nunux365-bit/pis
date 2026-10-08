"""contract_rate_line: is_active for soft retire without FK breaks.

Revision ID: 015_contract_rate_line_is_active
Revises: 014_contract_terms_version_billing_profile
Create Date: 2026-04-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015_contract_rate_line_is_active"
down_revision: Union[str, None] = "014_contract_terms_version_billing_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contract_rate_line",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("contract_rate_line", "is_active")
