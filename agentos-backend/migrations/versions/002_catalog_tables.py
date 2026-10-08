"""catalog agents and skills tables

Revision ID: 002_catalog
Revises: 001_initial
Create Date: 2026-03-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "002_catalog"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_agents",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("ring", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tasks_today", sa.Integer(), nullable=False),
        sa.Column("uptime_pct", sa.Numeric(6, 3), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_catalog_agents_slug"),
    )
    op.create_table(
        "catalog_skill_categories",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_catalog_skill_categories_name"),
    )
    op.create_table(
        "catalog_skills",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["catalog_skill_categories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_catalog_skills_slug"),
    )
    op.create_index(
        "ix_catalog_skills_category_id",
        "catalog_skills",
        ["category_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_catalog_skills_category_id", table_name="catalog_skills")
    op.drop_table("catalog_skills")
    op.drop_table("catalog_skill_categories")
    op.drop_table("catalog_agents")
