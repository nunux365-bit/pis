"""encrypt payroll workflow queue columns

Revision ID: 054_encrypt_payroll_workflow_queue_columns
Revises: 053_responder_eval_denormalized_storage
Create Date: 2026-08-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '054_encrypt_payroll_workflow_queue_columns'
down_revision: Union[str, None] = '053_responder_eval_denormalized_storage'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Alter column lengths to 255 for base64 encrypted ciphertexts
    op.alter_column('payroll_workflow_queue', 'empCode', type_=sa.String(length=255))
    op.alter_column('payroll_workflow_queue', 'empName', type_=sa.String(length=255))
    op.alter_column('payroll_workflow_queue', 'amount', type_=sa.String(length=255))
    op.alter_column('payroll_workflow_queue', 'overtimeHours', type_=sa.String(length=255))
    op.alter_column('payroll_workflow_queue', 'holidayDate', type_=sa.String(length=255))
    op.alter_column('payroll_workflow_queue', 'remarks', type_=sa.String(length=255))


def downgrade() -> None:
    # Revert column lengths
    op.alter_column('payroll_workflow_queue', 'empCode', type_=sa.String(length=100))
    op.alter_column('payroll_workflow_queue', 'empName', type_=sa.String(length=255))
    op.alter_column('payroll_workflow_queue', 'amount', type_=sa.String(length=100))
    op.alter_column('payroll_workflow_queue', 'overtimeHours', type_=sa.String(length=100))
    op.alter_column('payroll_workflow_queue', 'holidayDate', type_=sa.String(length=100))
    op.alter_column('payroll_workflow_queue', 'remarks', type_=sa.String(length=255))
