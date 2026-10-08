"""Named row claims for skip-locked writers (refsync prune, Prosight actionables).

Revision ID: 063_resource_claims
Revises: 062_responder_eval_list_and_prefix_search_indexes
Create Date: 2026-08-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "063_resource_claims"
down_revision: Union[str, None] = "062_responder_eval_list_and_prefix_search_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resource_claims",
        sa.Column("name", sa.String(length=200), primary_key=True),
    )
    # Seed so writers SKIP LOCKED without INSERT-on-conflict (which waits).
    claims = sa.table("resource_claims", sa.column("name", sa.String))
    op.bulk_insert(
        claims,
        [{"name": n} for n in (
            "prosight:actionables",
            "refsync:material",
            "refsync:vendor",
            "refsync:material_group",
            "refsync:service_group",
            "refsync:plant",
            "refsync:purchasing_group",
            "refsync:storage_location",
            "refsync:cost_center",
            "refsync:service",
            "refsync:asset",
            "refsync:tax_code",
        )],
    )


def downgrade() -> None:
    op.drop_table("resource_claims")
