"""create payroll routing matrix table

Revision ID: 046_create_payroll_routing_matrix_table
Revises: 045_create_payroll_tables
Create Date: 2026-07-06 15:58:22.552439

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '046_create_payroll_routing_matrix_table'
down_revision: Union[str, None] = '045_create_payroll_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table_name: str) -> bool:
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, 'payroll_routing_matrix'):
        op.create_table(
            'payroll_routing_matrix',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('earning_head', sa.String(length=255), nullable=False),
            sa.Column('employee_home', sa.String(length=255), nullable=False),
            sa.Column('initiators', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column('hrbps', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column('approvers', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, 'payroll_routing_matrix'):
        op.drop_table('payroll_routing_matrix')
