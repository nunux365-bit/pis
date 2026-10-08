"""user_oauth_tokens for Google Workspace tool OAuth

Revision ID: 004_oauth
Revises: 003_post_hitl_outbox
Create Date: 2026-03-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "004_oauth"
down_revision: Union[str, None] = "003_post_hitl_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_oauth_tokens",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("credential_json", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("account_email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_oauth_tokens_user_provider"),
    )
    op.create_index(
        "ix_user_oauth_tokens_user_provider",
        "user_oauth_tokens",
        ["user_id", "provider"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_oauth_tokens_user_provider", table_name="user_oauth_tokens")
    op.drop_table("user_oauth_tokens")
