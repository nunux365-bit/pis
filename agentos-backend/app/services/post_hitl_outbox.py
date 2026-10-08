"""Enqueue rows after HITL approval completes a workflow (ERP / SAP handoff)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Approval,
    ApprovalStatus,
    PostHitlOutbox,
    PostHitlOutboxStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.services.workflow_runner import (
    canonical_workflow_key,
    workflow_eligible_for_post_hitl_erp,
)


def _approval_snapshot(approval: Approval) -> dict:
    decided = approval.decided_at
    return {
        "id": str(approval.id),
        "title": approval.title,
        "status": approval.status,
        "payload": approval.payload,
        "decided_at": decided.isoformat() if isinstance(decided, datetime) else None,
    }


async def enqueue_post_hitl_after_approval(db: AsyncSession, approval: Approval) -> None:
    """Idempotent: one outbox row per approval (unique on approval_id)."""
    if approval.status != ApprovalStatus.APPROVED.value:
        return
    pl = approval.payload or {}
    raw = pl.get("workflow_run_id")
    if not raw:
        return
    try:
        wid = UUID(str(raw))
    except (ValueError, TypeError):
        return

    dup = await db.execute(
        select(PostHitlOutbox.id).where(PostHitlOutbox.approval_id == approval.id).limit(1)
    )
    if dup.scalar_one_or_none():
        return

    run = await db.get(WorkflowRun, wid)
    if not run or run.status != WorkflowRunStatus.COMPLETED.value:
        return

    # Assignee must own the workflow run (blocks forged payload on self-assigned approvals).
    if approval.assignee_user_id != run.user_id:
        return

    wk = (run.workflow_key or "").strip()
    if not workflow_eligible_for_post_hitl_erp(wk):
        return
    snapshot = {
        "approval": _approval_snapshot(approval),
        "workflow_run": {
            "id": str(run.id),
            "workflow_key": wk,
            "canonical_workflow_key": canonical_workflow_key(wk),
            "input_data": run.input_data,
            "output_data": run.output_data,
        },
    }
    db.add(
        PostHitlOutbox(
            user_id=run.user_id,
            workflow_run_id=run.id,
            approval_id=approval.id,
            workflow_key=wk or "unknown",
            canonical_workflow_key=canonical_workflow_key(wk),
            status=PostHitlOutboxStatus.PENDING.value,
            snapshot=snapshot,
        )
    )
