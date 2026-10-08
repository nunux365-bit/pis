"""Strip the retired `prosight_user` value from users.role.

Revision ID: 060_drop_prosight_user_role
Revises: 059_prosight_actionables
Create Date: 2026-08-18

Prosight is open to every authenticated (1mg SSO) user, so `prosight_user` never
gated anything — see `FEATURE_ACCESS` in app/security/rbac.py and
docs/prosight-login-access.md §9, which lists this cleanup as the follow-up to
removing the grant/revoke endpoints.

The value is already gone from the `UserRole` enum, so `normalize_roles` filters
it out on read and the application cannot see it either way. This migration only
stops the dead string from sitting in the column and reappearing in admin
listings or exports.

Safety: the JSONB `role` array is rewritten with the value removed, preserving
order and every other role. A user left with an empty array gets `["employee"]`,
matching the column default and what `normalize_roles` would have returned for
them anyway — so no one can end up role-less.

Downgrade is intentionally a no-op: which users carried a role that granted
nothing is not worth reconstructing, and re-adding it would resurrect a value
the enum no longer accepts.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "060_drop_prosight_user_role"
down_revision: Union[str, None] = "059_prosight_actionables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE = "prosight_user"


def upgrade() -> None:
    bind = op.get_bind()

    # jsonb_agg over the filtered elements, coalesced so a user whose only role
    # was `prosight_user` lands on the column default rather than an empty array.
    result = bind.execute(
        sa.text(
            """
            UPDATE users
               SET role = COALESCE(
                       (
                           SELECT jsonb_agg(elem ORDER BY ord)
                             FROM jsonb_array_elements_text(role)
                                  WITH ORDINALITY AS r(elem, ord)
                            WHERE elem <> CAST(:role AS text)
                       ),
                       '["employee"]'::jsonb
                   )
             WHERE role @> jsonb_build_array(CAST(:role AS text))
            """
        ),
        {"role": _ROLE},
    )
    print(f"060: stripped '{_ROLE}' from {result.rowcount} user row(s)")


def downgrade() -> None:
    """No-op — see the module docstring."""
