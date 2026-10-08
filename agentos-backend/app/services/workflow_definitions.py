"""Load workflow_definitions rows for triggers and catalog APIs."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkflowDefinition


async def fetch_workflow_definition(
    db: AsyncSession, workflow_key: str
) -> WorkflowDefinition | None:
    k = (workflow_key or "").strip().lower()
    if not k:
        return None
    r = await db.execute(
        select(WorkflowDefinition).where(
            func.lower(WorkflowDefinition.workflow_key) == k,
            WorkflowDefinition.enabled.is_(True),
        )
    )
    return r.scalar_one_or_none()
