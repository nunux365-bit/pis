"""add_last_login_at_to_users

Revision ID: 044_add_last_login_at_to_users
Revises: 043_responder_eval_hardening
Create Date: 2026-07-06 16:50:21.478594

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '044_add_last_login_at_to_users'
down_revision: Union[str, None] = '043_responder_eval_hardening'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    insp = sa.inspect(bind)
    columns = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, 'users', 'last_login_at'):
        op.add_column('users', sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, 'users', 'last_login_at'):
        op.drop_column('users', 'last_login_at')
