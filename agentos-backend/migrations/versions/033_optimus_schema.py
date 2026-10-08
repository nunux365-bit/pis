"""Optimus tables for SmartQnA, conversations, and Flock integration.

Creates:
- optimus_documents (with deferred ingestion support)
- optimus_sections (document hierarchy)
- optimus_conversations (chat sessions)
- optimus_messages (chat messages)
- optimus_flock_accounts (Flock user linking)

Revision ID: 033_optimus_schema
Revises: 032_users_role_jsonb
Create Date: 2025-06-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "033_optimus_schema"
down_revision: Union[str, None] = "032_users_role_jsonb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(bind, table_name: str) -> bool:
    insp = sa.inspect(bind)
    return table_name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    # ─────────────────────────────────────────────────────────────────────────
    # optimus_documents — Document metadata with deferred ingestion support
    # ─────────────────────────────────────────────────────────────────────────
    if not _table_exists(bind, "optimus_documents"):
        op.create_table(
            "optimus_documents",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("filename", sa.String(length=512), nullable=False),
            sa.Column("file_hash", sa.String(length=64), nullable=False),
            sa.Column("file_path", sa.String(length=1024), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="uploaded"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("topics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("page_count", sa.Integer(), nullable=True),
            sa.Column("chunk_count", sa.Integer(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("file_hash", name="uq_optimus_documents_file_hash"),
        )
        op.create_index("ix_optimus_documents_status", "optimus_documents", ["status"])
        op.create_index("ix_optimus_documents_filename", "optimus_documents", ["filename"])

    # ─────────────────────────────────────────────────────────────────────────
    # optimus_sections — Document sections with hierarchy
    # ─────────────────────────────────────────────────────────────────────────
    if not _table_exists(bind, "optimus_sections"):
        op.create_table(
            "optimus_sections",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("title", sa.String(length=500), nullable=True),
            sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["document_id"],
                ["optimus_documents.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["parent_id"],
                ["optimus_sections.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_optimus_sections_document", "optimus_sections", ["document_id"])
        op.create_index("ix_optimus_sections_parent", "optimus_sections", ["parent_id"])

    # ─────────────────────────────────────────────────────────────────────────
    # optimus_conversations — Chat sessions for Optimus services
    # ─────────────────────────────────────────────────────────────────────────
    if not _table_exists(bind, "optimus_conversations"):
        op.create_table(
            "optimus_conversations",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "title", sa.String(length=500), nullable=False, server_default="New conversation"
            ),
            sa.Column("service", sa.String(length=32), nullable=False, server_default="smartqna"),
            sa.Column("channel", sa.String(length=32), nullable=False, server_default="web"),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_optimus_conversations_user_updated",
            "optimus_conversations",
            ["user_id", "updated_at"],
        )
        op.create_index("ix_optimus_conversations_service", "optimus_conversations", ["service"])
        op.create_index("ix_optimus_conversations_channel", "optimus_conversations", ["channel"])

    # ─────────────────────────────────────────────────────────────────────────
    # optimus_messages — Individual messages in conversations
    # ─────────────────────────────────────────────────────────────────────────
    if not _table_exists(bind, "optimus_messages"):
        op.create_table(
            "optimus_messages",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("confidence", sa.String(length=32), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["optimus_conversations.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_optimus_messages_conversation_created",
            "optimus_messages",
            ["conversation_id", "created_at"],
        )

    # ─────────────────────────────────────────────────────────────────────────
    # optimus_flock_accounts — Flock user to AgentOS user mapping
    # ─────────────────────────────────────────────────────────────────────────
    if not _table_exists(bind, "optimus_flock_accounts"):
        op.create_table(
            "optimus_flock_accounts",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("flock_user_id", sa.String(length=128), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("flock_token", sa.String(length=128), nullable=True),
            sa.Column("flock_email", sa.String(length=320), nullable=True),
            sa.Column("flock_name", sa.String(length=256), nullable=True),
            sa.Column(
                "linked_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("flock_user_id", name="uq_optimus_flock_accounts_flock_user"),
            sa.UniqueConstraint("user_id", name="uq_optimus_flock_accounts_user"),
        )
        op.create_index(
            "ix_optimus_flock_accounts_flock_user", "optimus_flock_accounts", ["flock_user_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Drop tables in reverse order (FK dependencies)
    if _table_exists(bind, "optimus_flock_accounts"):
        op.drop_index("ix_optimus_flock_accounts_flock_user", table_name="optimus_flock_accounts")
        op.drop_table("optimus_flock_accounts")

    if _table_exists(bind, "optimus_messages"):
        op.drop_index("ix_optimus_messages_conversation_created", table_name="optimus_messages")
        op.drop_table("optimus_messages")

    if _table_exists(bind, "optimus_conversations"):
        op.drop_index("ix_optimus_conversations_channel", table_name="optimus_conversations")
        op.drop_index("ix_optimus_conversations_service", table_name="optimus_conversations")
        op.drop_index("ix_optimus_conversations_user_updated", table_name="optimus_conversations")
        op.drop_table("optimus_conversations")

    if _table_exists(bind, "optimus_sections"):
        op.drop_index("ix_optimus_sections_parent", table_name="optimus_sections")
        op.drop_index("ix_optimus_sections_document", table_name="optimus_sections")
        op.drop_table("optimus_sections")

    if _table_exists(bind, "optimus_documents"):
        op.drop_index("ix_optimus_documents_filename", table_name="optimus_documents")
        op.drop_index("ix_optimus_documents_status", table_name="optimus_documents")
        op.drop_table("optimus_documents")
