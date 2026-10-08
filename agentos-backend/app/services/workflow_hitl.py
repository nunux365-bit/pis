"""Link workflow runs to human-in-the-loop approvals (payload.workflow_run_id)."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ApprovalStatus, WorkflowRun, WorkflowRunStatus


async def sync_workflow_run_from_approval_decision(
    db: AsyncSession, approval: Approval
) -> None:
    """When an approval is decided, complete or cancel linked workflow_run if any."""
    pl = approval.payload or {}
    raw = pl.get("workflow_run_id")
    if not raw:
        return
    try:
        wid = UUID(str(raw))
    except (ValueError, TypeError):
        return
    run = await db.get(WorkflowRun, wid)
    if not run or run.user_id != approval.assignee_user_id:
        return
    if run.status != WorkflowRunStatus.AWAITING_HITL.value:
        return

    if approval.status == ApprovalStatus.APPROVED.value:
        run.status = WorkflowRunStatus.COMPLETED.value
        prev = dict(run.output_data) if run.output_data else {}
        run.output_data = {
            **prev,
            "status": "ok",
            "message": "Workflow completed after human approval (HITL).",
            "approval_id": str(approval.id),
            "hitl_gate": pl.get("hitl_gate"),
        }
        run.error_message = None
    elif approval.status == ApprovalStatus.REJECTED.value:
        run.status = WorkflowRunStatus.CANCELLED.value
        run.error_message = "Human rejected at HITL gate."
        prev = dict(run.output_data) if run.output_data else {}
        run.output_data = {**prev, "approval_id": str(approval.id), "hitl_rejected": True}
