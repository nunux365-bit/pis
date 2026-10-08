"""contract_rate_line: site override of a shared (NULL-site) line.

Revision ID: 066_crl_overrides_contract_rate_line
Revises: 065_add_neeraj_singh_and_test_route
Create Date: 2026-08-25
"""

from typing import Sequence, Union

from alembic import op

revision: str = "066_crl_overrides_contract_rate_line"
down_revision: Union[str, None] = "065_add_neeraj_singh_and_test_route"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE contract_rate_line
          ADD COLUMN IF NOT EXISTS overrides_contract_rate_line_id UUID
          REFERENCES contract_rate_line (id) ON DELETE SET NULL;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_crl_site_override_of
        ON contract_rate_line (service_site_id, overrides_contract_rate_line_id)
        WHERE overrides_contract_rate_line_id IS NOT NULL
          AND service_site_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_crl_site_override_of;")
    op.execute(
        "ALTER TABLE contract_rate_line DROP COLUMN IF EXISTS overrides_contract_rate_line_id;"
    )
