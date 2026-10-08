"""merge heads: prosight actionables/role-drop + responder_eval order_id index

Revision ID: 061_merge_prosight_responder_eval_heads
Revises: 060_drop_prosight_user_role, 059_responder_eval_order_id_search_index
Create Date: 2026-08-19

Two independent migrations both chained onto 058_responder_eval_chart_window_index
and landed on main as separate heads:
- 059_prosight_actionables -> 060_drop_prosight_user_role (PR #44)
- 059_responder_eval_order_id_search_index

Neither is renumbered here since either revision id may already be recorded in
an alembic_version table in some environment. This merge revision has both
heads as parents and does nothing itself, restoring a single head so
`alembic upgrade head` is unambiguous again.
"""

from typing import Sequence, Union

revision: str = "061_merge_prosight_responder_eval_heads"
down_revision: Union[str, tuple[str, ...], None] = (
    "060_drop_prosight_user_role",
    "059_responder_eval_order_id_search_index",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
