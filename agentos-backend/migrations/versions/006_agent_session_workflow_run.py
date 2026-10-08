"""agent_sessions.workflow_run_id — link LangGraph thread to WorkflowRun

Revision ID: 006_session_run
Revises: 005_workflows
Create Date: 2026-03-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "006_session_run"
down_revision: Union[str, None] = "005_workflows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("workflow_run_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_sessions_workflow_run_id",
        "agent_sessions",
        "workflow_runs",
        ["workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_sessions_workflow_run_id",
        "agent_sessions",
        ["workflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_workflow_run_id", table_name="agent_sessions")
    op.drop_constraint(
        "fk_agent_sessions_workflow_run_id",
        "agent_sessions",
        type_="foreignkey",
    )
    op.drop_column("agent_sessions", "workflow_run_id")
