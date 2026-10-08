"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-03-20

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.db.base import Base
    from app.db import models  # noqa: F401

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    from app.db.base import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
