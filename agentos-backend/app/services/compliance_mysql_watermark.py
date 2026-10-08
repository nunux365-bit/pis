"""Max ``calls.id`` already successfully ingested (completed mysql_call workflow runs)."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import WorkflowRun, WorkflowRunStatus
from app.db.session import AsyncSessionLocal

_COMPLIANCE_WORKFLOW_KEY = "compliance_call"


async def max_completed_mysql_call_id() -> int:
    """Largest MySQL ``calls.id`` with a completed ``compliance_call`` run (uses ``workflow_runs.mysql_call_id``)."""
    async with AsyncSessionLocal() as db:
        q = select(func.coalesce(func.max(WorkflowRun.mysql_call_id), 0)).where(
            WorkflowRun.workflow_key == _COMPLIANCE_WORKFLOW_KEY,
            WorkflowRun.status == WorkflowRunStatus.COMPLETED.value,
            WorkflowRun.mysql_call_id.isnot(None),
        )
        row = (await db.execute(q)).scalar_one()
    return int(row or 0)
