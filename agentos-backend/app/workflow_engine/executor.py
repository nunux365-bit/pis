"""Execute composable workflow definitions (sequential steps + HITL)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Approval,
    ApprovalStatus,
    Notification,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.services.audit import write_audit
from app.workflow_engine.registry import get_step_handler

log = logging.getLogger(__name__)


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    return obj


async def run_composable_pipeline(
    db: AsyncSession,
    run: WorkflowRun,
    definition: WorkflowDefinition,
) -> None:
    """
    Mutates `run` in place: output_data, status, approval row.
    Caller must commit.
    """
    steps = definition.steps
    if not isinstance(steps, list) or not steps:
        raise ValueError("definition has no steps")

    accum: dict[str, Any] = {"input": dict(run.input_data or {})}
    pipeline_log: list[dict[str, Any]] = []

    for step in steps:
        sid = step.get("id") or "step"
        stype = step.get("type")
        if not stype:
            raise ValueError(f"step {sid} missing type")
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        handler = get_step_handler(stype)
        try:
            out = await handler(accum, config, db, run.user_id, run)
        except Exception as e:
            log.exception("pipeline step %s failed", sid)
            run.status = WorkflowRunStatus.FAILED.value
            run.error_message = f"step {sid}: {e}"[:2000]
            run.output_data = {
                "phase": "pipeline_failed",
                "accum": _json_safe({k: v for k, v in accum.items() if k != "input"}),
                "failed_step": sid,
                "pipeline_log": pipeline_log,
            }
            return

        accum[sid] = out
        pipeline_log.append({"id": sid, "type": stype, "ok": True})

        if isinstance(out, dict) and out.get("_hitl"):
            title = out["title"]
            desc = out.get("description") or ""
            appr = Approval(
                assignee_user_id=run.user_id,
                created_by_user_id=run.user_id,
                title=title,
                description=desc,
                status=ApprovalStatus.PENDING.value,
                agent_name=out.get("agent_name") or "Workflow Kernel",
                amount=None,
                confidence=None,
                risk="medium",
                payload={
                    "workflow_run_id": str(run.id),
                    "hitl_gate": out.get("hitl_gate"),
                    "workflow_key": run.workflow_key,
                    "expanded_detail": desc,
                    "pipeline_context": out.get("payload_extra") or {},
                    "definition_version": definition.version,
                },
            )
            db.add(appr)
            await db.flush()
            run.status = WorkflowRunStatus.AWAITING_HITL.value
            run.output_data = {
                "phase": "awaiting_human",
                "pipeline": {
                    "definition_key": definition.workflow_key,
                    "definition_version": definition.version,
                    "steps_completed": [x["id"] for x in pipeline_log],
                    "accum": _json_safe(
                        {k: v for k, v in accum.items() if k != "input" and not str(k).startswith("_")}
                    ),
                },
                "message": "Pending your confirmation in Approvals.",
                "pending_approval_id": str(appr.id),
            }
            db.add(
                Notification(
                    user_id=run.user_id,
                    category="approval",
                    title=f"Workflow {run.workflow_key} needs your confirmation",
                    body="Open the review queue (O2C / S2P) to complete the human-in-the-loop step.",
                    link="/o2c/ohc-mis",
                )
            )
            await write_audit(
                db,
                actor_user_id=run.user_id,
                action="workflow.pipeline.awaiting_hitl",
                resource_type="workflow_run",
                resource_id=str(run.id),
                details={"approval_id": str(appr.id), "definition": definition.workflow_key},
            )
            return

    # No HITL step — mark completed (rare)
    run.status = WorkflowRunStatus.COMPLETED.value
    run.output_data = {
        "phase": "pipeline_complete",
        "pipeline_log": pipeline_log,
        "accum": _json_safe({k: v for k, v in accum.items() if k != "input"}),
    }
