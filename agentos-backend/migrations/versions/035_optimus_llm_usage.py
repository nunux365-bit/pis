"""Optimus LLM usage tracking table.

Revision ID: 035_optimus_llm_usage
Revises: 034_prosight_snapshots
Create Date: 2026-06-18

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "035_optimus_llm_usage"
down_revision = "034_prosight_snapshots"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name: str) -> bool:
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "optimus_llm_usage"):
        op.create_table(
            "optimus_llm_usage",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "conversation_id",
                UUID(as_uuid=True),
                sa.ForeignKey("optimus_conversations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("step", sa.String(64), nullable=False),
            sa.Column("model", sa.String(64), nullable=False),
            sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

        # Indexes for common queries
        op.create_index(
            "ix_optimus_llm_usage_conversation",
            "optimus_llm_usage",
            ["conversation_id"],
        )
        op.create_index(
            "ix_optimus_llm_usage_user",
            "optimus_llm_usage",
            ["user_id"],
        )
        op.create_index(
            "ix_optimus_llm_usage_created",
            "optimus_llm_usage",
            ["created_at"],
        )
        op.create_index(
            "ix_optimus_llm_usage_step",
            "optimus_llm_usage",
            ["step"],
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _table_exists(bind, "optimus_llm_usage"):
        op.drop_index("ix_optimus_llm_usage_step", table_name="optimus_llm_usage")
        op.drop_index("ix_optimus_llm_usage_created", table_name="optimus_llm_usage")
        op.drop_index("ix_optimus_llm_usage_user", table_name="optimus_llm_usage")
        op.drop_index("ix_optimus_llm_usage_conversation", table_name="optimus_llm_usage")
        op.drop_table("optimus_llm_usage")
