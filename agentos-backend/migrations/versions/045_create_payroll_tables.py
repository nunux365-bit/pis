"""create_payroll_tables

Revision ID: 045_create_payroll_tables
Revises: 044_add_last_login_at_to_users
Create Date: 2026-07-06 12:56:53.121139

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '045_create_payroll_tables'
down_revision: Union[str, None] = '044_add_last_login_at_to_users'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table_name: str) -> bool:
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def _index_exists(bind, table_name: str, index_name: str) -> bool:
    insp = sa.inspect(bind)
    return any(idx["name"] == index_name for idx in insp.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()

    # Create payroll_workflow_queue
    if not _table_exists(bind, 'payroll_workflow_queue'):
        op.create_table(
            'payroll_workflow_queue',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('empCode', sa.String(length=100), nullable=True),
            sa.Column('empName', sa.String(length=255), nullable=True),
            sa.Column('grade', sa.String(length=50), nullable=True),
            sa.Column('designation', sa.String(length=150), nullable=True),
            sa.Column('employeeHome', sa.String(length=255), nullable=True),
            sa.Column('type', sa.String(length=100), nullable=True),
            sa.Column('module', sa.String(length=100), nullable=True),
            sa.Column('amount', sa.String(length=100), nullable=True),
            sa.Column('overtimeHours', sa.String(length=100), nullable=True),
            sa.Column('holidayDate', sa.String(length=100), nullable=True),
            sa.Column('remarks', sa.String(length=255), nullable=True),
            sa.Column('status', sa.String(length=50), nullable=False),
            sa.Column('hrbpComments', sa.Text(), nullable=True),
            sa.Column('hodComments', sa.Text(), nullable=True),
            sa.Column('flaggedColumns', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('history', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column('initiatorEmail', sa.String(length=255), nullable=True),
            sa.Column('initiatorEmpCode', sa.String(length=100), nullable=True),
            sa.Column('effectiveFrom', sa.String(length=100), nullable=True),
            sa.Column('effectiveTo', sa.String(length=100), nullable=True),
            sa.Column('paymentMonth', sa.String(length=50), nullable=True),
            sa.Column('closedAt', sa.String(length=100), nullable=True),
            sa.Column('closedByEmail', sa.String(length=255), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_payroll_workflow_queue_empCode', 'payroll_workflow_queue', ['empCode'], unique=False)
        op.create_index('ix_payroll_workflow_queue_module', 'payroll_workflow_queue', ['module'], unique=False)
        op.create_index('ix_payroll_workflow_queue_status', 'payroll_workflow_queue', ['status'], unique=False)
        op.create_index('ix_payroll_workflow_queue_initiatorEmail', 'payroll_workflow_queue', ['initiatorEmail'], unique=False)
        op.create_index('ix_payroll_workflow_queue_closedAt', 'payroll_workflow_queue', ['closedAt'], unique=False)
        op.create_index('ix_payroll_workflow_queue_status_module', 'payroll_workflow_queue', ['status', 'module'], unique=False)
        op.create_index('ix_payroll_workflow_queue_status_closed', 'payroll_workflow_queue', ['status', 'closedAt'], unique=False)

    # Composite indexes for the (status, module, employeeHome) sheet-stage
    # lookups used by every maker/hrbp/hod handler, and the (status, module,
    # closedAt) filter+sort used by the paginated closed-sheet query.
    # Guarded independently of table creation so they also apply if this
    # migration already ran (table exists) before these indexes were added.
    if _table_exists(bind, 'payroll_workflow_queue'):
        if not _index_exists(bind, 'payroll_workflow_queue', 'ix_payroll_workflow_queue_status_module_home'):
            op.create_index(
                'ix_payroll_workflow_queue_status_module_home',
                'payroll_workflow_queue',
                ['status', 'module', 'employeeHome'],
                unique=False,
            )
        if not _index_exists(bind, 'payroll_workflow_queue', 'ix_payroll_workflow_queue_status_module_closed'):
            op.create_index(
                'ix_payroll_workflow_queue_status_module_closed',
                'payroll_workflow_queue',
                ['status', 'module', 'closedAt'],
                unique=False,
            )

    # Create payroll_audit_history
    if not _table_exists(bind, 'payroll_audit_history'):
        op.create_table(
            'payroll_audit_history',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('action', sa.String(length=255), nullable=False),
            sa.Column('user', sa.String(length=255), nullable=False),
            sa.Column('remarks', sa.Text(), nullable=True),
            sa.Column('timestamp', sa.String(length=100), nullable=False),
            sa.PrimaryKeyConstraint('id')
        )

    # Create payroll_notifications
    if not _table_exists(bind, 'payroll_notifications'):
        op.create_table(
            'payroll_notifications',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('userEmail', sa.String(length=255), nullable=False),
            sa.Column('text', sa.String(length=500), nullable=False),
            sa.Column('timestamp', sa.String(length=100), nullable=False),
            sa.Column('isRead', sa.Integer(), nullable=False, server_default='0'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_payroll_notifications_userEmail', 'payroll_notifications', ['userEmail'], unique=False)
        op.create_index('ix_payroll_notifications_isRead', 'payroll_notifications', ['isRead'], unique=False)

    # Create payroll_workflow_config
    if not _table_exists(bind, 'payroll_workflow_config'):
        op.create_table(
            'payroll_workflow_config',
            sa.Column('key', sa.String(length=50), nullable=False),
            sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.PrimaryKeyConstraint('key')
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, 'payroll_workflow_config'):
        op.drop_table('payroll_workflow_config')
        
    if _table_exists(bind, 'payroll_notifications'):
        op.drop_index('ix_payroll_notifications_isRead', table_name='payroll_notifications')
        op.drop_index('ix_payroll_notifications_userEmail', table_name='payroll_notifications')
        op.drop_table('payroll_notifications')
        
    if _table_exists(bind, 'payroll_audit_history'):
        op.drop_table('payroll_audit_history')
        
    if _table_exists(bind, 'payroll_workflow_queue'):
        if _index_exists(bind, 'payroll_workflow_queue', 'ix_payroll_workflow_queue_status_module_closed'):
            op.drop_index('ix_payroll_workflow_queue_status_module_closed', table_name='payroll_workflow_queue')
        if _index_exists(bind, 'payroll_workflow_queue', 'ix_payroll_workflow_queue_status_module_home'):
            op.drop_index('ix_payroll_workflow_queue_status_module_home', table_name='payroll_workflow_queue')
        op.drop_index('ix_payroll_workflow_queue_status', table_name='payroll_workflow_queue')
        op.drop_index('ix_payroll_workflow_queue_module', table_name='payroll_workflow_queue')
        op.drop_index('ix_payroll_workflow_queue_empCode', table_name='payroll_workflow_queue')
        op.drop_index('ix_payroll_workflow_queue_initiatorEmail', table_name='payroll_workflow_queue')
        op.drop_index('ix_payroll_workflow_queue_closedAt', table_name='payroll_workflow_queue')
        op.drop_index('ix_payroll_workflow_queue_status_module', table_name='payroll_workflow_queue')
        op.drop_index('ix_payroll_workflow_queue_status_closed', table_name='payroll_workflow_queue')
        op.drop_table('payroll_workflow_queue')
