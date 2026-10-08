"""Store user RBAC roles as JSONB array (column name remains ``role``).

Revision ID: 032_users_role_jsonb
Revises: 031_cold_outreach_feature
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "032_users_role_jsonb"
down_revision: Union[str, None] = "031_cold_outreach_feature"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VALID_ROLES_SQL = (
    "'system_admin', 'dept_head', 'reviewer', 'employee', "
    "'glp_compliance_reviewer', 'email_agent_access'"
)


def upgrade() -> None:
    # 1) Drop varchar default — PostgreSQL cannot auto-cast it during TYPE change.
    op.alter_column(
        "users",
        "role",
        server_default=None,
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )

    # 2) Column type: varchar(32) → jsonb (wrap each existing scalar in a one-element array).
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=32),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="jsonb_build_array(role)",
    )

    # 3) Default for new rows.
    op.alter_column(
        "users",
        "role",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[\"employee\"]'::jsonb"),
        existing_nullable=False,
    )

    # 4) Row updates: null / non-array / empty array → ["employee"].
    op.execute(
        """
        UPDATE users
        SET role = '["employee"]'::jsonb
        WHERE role IS NULL
           OR jsonb_typeof(role) <> 'array'
           OR jsonb_array_length(role) = 0
        """
    )

    # 5) Row updates: keep only known role tokens; fall back to employee if none remain.
    op.execute(
        f"""
        UPDATE users u
        SET role = COALESCE(
            (
                SELECT jsonb_agg(DISTINCT tok ORDER BY tok)
                FROM (
                    SELECT lower(trim(elem)) AS tok
                    FROM jsonb_array_elements_text(u.role) AS elem
                    WHERE lower(trim(elem)) IN ({_VALID_ROLES_SQL})
                ) s
            ),
            '["employee"]'::jsonb
        )
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(u.role) AS elem
            WHERE lower(trim(elem)) NOT IN ({_VALID_ROLES_SQL})
               OR trim(elem) = ''
        )
        """
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "role",
        server_default=None,
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
    )

    op.alter_column(
        "users",
        "role",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.String(length=32),
        existing_nullable=False,
        postgresql_using="COALESCE(NULLIF(trim(role->>0), ''), 'employee')",
    )

    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=32),
        server_default=sa.text("'employee'"),
        existing_nullable=False,
    )
