"""Workflow-run rollups for the home dashboard.

Kept scoped to ``WorkflowRun.user_id == current user`` so the numbers align
with what the Sessions page shows the same user. Two aggregates:

* status distribution (queued / running / awaiting_hitl / completed / failed / cancelled)
* top failure hot-spots by ``workflow_key`` over 30d
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, WorkflowRun, WorkflowRunStatus


async def workflow_status_distribution(
    db: AsyncSession, user: User, *, days: int = 30
) -> dict[str, int]:
    """Runs by status, created in the last ``days`` for the current user."""

    since = datetime.now(UTC) - timedelta(days=days)
    q = (
        select(WorkflowRun.status, func.count())
        .where(
            WorkflowRun.user_id == user.id,
            WorkflowRun.created_at >= since,
        )
        .group_by(WorkflowRun.status)
    )
    rows = (await db.execute(q)).all()
    known = {st.value: 0 for st in WorkflowRunStatus}
    for status_, cnt in rows:
        if status_ in known:
            known[status_] = int(cnt or 0)
        else:
            known[str(status_)] = int(cnt or 0)
    return known


async def workflow_failures_by_key(
    db: AsyncSession, user: User, *, days: int = 30, limit: int = 6
) -> list[dict[str, Any]]:
    """Failure hot-spots: ``workflow_key`` with most ``failed`` runs in the last ``days``."""

    if limit <= 0:
        return []
    since = datetime.now(UTC) - timedelta(days=days)
    q = (
        select(WorkflowRun.workflow_key, func.count().label("n"))
        .where(
            WorkflowRun.user_id == user.id,
            WorkflowRun.status == WorkflowRunStatus.FAILED.value,
            WorkflowRun.created_at >= since,
        )
        .group_by(WorkflowRun.workflow_key)
        .order_by(func.count().desc())
        .limit(limit)
    )
    rows = (await db.execute(q)).all()
    return [{"workflow_key": key or "unknown", "failed": int(cnt or 0)} for key, cnt in rows]


__all__ = [
    "workflow_status_distribution",
    "workflow_failures_by_key",
]
