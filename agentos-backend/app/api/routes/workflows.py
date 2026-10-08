"""Workflow runs — automation phase (skills/LangGraph) then HITL via Approval."""

import asyncio
import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config.settings import settings
from app.db.models import (
    Approval,
    ApprovalStatus,
    Notification,
    User,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.db.session import AsyncSessionLocal, get_db
from app.services.audit import write_audit
from app.services.workflow_definitions import fetch_workflow_definition
from app.services.workflow_catalog import list_workflow_catalog
from app.services.workflow_runner import (
    COMPLIANCE_CALL_KEYS,
    HR_CONTRACTOR_KEYS,
    INVOICE_3WAY_KEYS,
    O2C_OHC_KEYS,
    run_automation_phase,
)
from app.workflow_engine.executor import run_composable_pipeline
from app.skills.finance.invoice_match import format_match_for_approval_detail
from app.skills.hr.contractor_billing import format_contractor_billing_detail

log = logging.getLogger(__name__)
router = APIRouter()


class TriggerBody(BaseModel):
    workflow_key: str = Field(..., min_length=1, max_length=120)
    payload: dict | None = None


async def _execute_workflow_run(run_id: UUID) -> None:
    """
    Phase 1: RUNNING — execute registered automation (invoice graph, etc.).
    Phase 2: AWAITING_HITL — create Approval; human completes via Approvals API.
    """
    await asyncio.sleep(0)
    try:
        wf_key: str | None = None
        wf_input: dict | None = None
        async with AsyncSessionLocal() as db:
            r = await db.get(WorkflowRun, run_id)
            if not r:
                return
            wf_key = r.workflow_key
            wf_input = (
                dict(r.input_data)
                if isinstance(r.input_data, dict)
                else None
            )
            r.status = WorkflowRunStatus.RUNNING.value
            await db.commit()

        if settings.workflow_composable_enabled:
            async with AsyncSessionLocal() as db:
                r = await db.get(WorkflowRun, run_id)
                if r and r.status == WorkflowRunStatus.RUNNING.value:
                    dfn = await fetch_workflow_definition(db, r.workflow_key)
                    if dfn:
                        await run_composable_pipeline(db, r, dfn)
                        await db.commit()
                        return

        automation = await run_automation_phase(
            wf_key or "",
            wf_input,
            thread_id=str(run_id),
        )

        async with AsyncSessionLocal() as db:
            r = await db.get(WorkflowRun, run_id)
            if not r or r.status != WorkflowRunStatus.RUNNING.value:
                return

            if (
                automation.get("handler") == "finance_langgraph_invoice_3way"
                and automation.get("graph_error")
                and not automation.get("match_result")
            ):
                r.status = WorkflowRunStatus.FAILED.value
                r.error_message = (automation.get("graph_error") or "graph_failed")[:2000]
                r.output_data = {"automation": automation, "phase": "failed"}
                await db.commit()
                return

            if (
                automation.get("handler") == "o2c_ohc_langgraph"
                and automation.get("graph_error")
                and automation.get("o2c_result") is None
            ):
                r.status = WorkflowRunStatus.FAILED.value
                r.error_message = (automation.get("graph_error") or "o2c_failed")[:2000]
                r.output_data = {"automation": automation, "phase": "failed"}
                await db.commit()
                return

            if automation.get("handler") == "compliance_call_batch":
                ge = automation.get("graph_error") or (
                    (automation.get("result") or {}).get("graph_error")
                    if isinstance(automation.get("result"), dict)
                    else None
                )
                r.output_data = {
                    "automation": automation,
                    "phase": "failed" if ge else "completed",
                }
                if ge:
                    r.status = WorkflowRunStatus.FAILED.value
                    r.error_message = str(ge)[:2000]
                else:
                    r.status = WorkflowRunStatus.COMPLETED.value
                    r.error_message = None
                await db.commit()
                return

            mr = automation.get("match_result")
            mr_dict = mr if isinstance(mr, dict) else None

            if automation.get("handler") == "o2c_ohc_langgraph":
                o2c = automation.get("o2c_result") if isinstance(automation.get("o2c_result"), dict) else {}
                pr = o2c.get("pipeline_result") or {}
                inv = o2c.get("invoice_build_results") or []
                skips = o2c.get("invoice_skips") or []
                errs = o2c.get("errors") or []
                title = "HITL — O2C OHC · Contracts + attendance billing"
                description = (
                    f"Phase A: candidates={pr.get('candidates', 0)} ok={pr.get('ok', 0)} "
                    f"failed={pr.get('failed', 0)}. "
                    f"Phase B: invoices={len(inv)} skipped_sites={len(skips)}."
                    + (f" Graph errors: {errs[:2]}" if errs else "")
                )
                expanded_raw = json.dumps(o2c, indent=2, default=str)
                expanded = expanded_raw if len(expanded_raw) <= 12000 else expanded_raw[:12000] + "\n…"
                agent_name = "O2C OHC Agent"
                mr_dict = None
            elif mr_dict:
                if mr_dict.get("skill") == "hr.contractor_billing":
                    contractor = str(mr_dict.get("contractor_name") or "Contractor")
                    conf = mr_dict.get("confidence")
                    rec = mr_dict.get("recommendation")
                    title = f"HITL — Contractor billing · {contractor}"
                    description = (
                        f"Confidence **{conf}%** · {rec or 'review'}. "
                        "Approve to complete this billing draft (UAT), or reject to cancel."
                    )
                    expanded = format_contractor_billing_detail(mr_dict)
                    agent_name = "HR Agent"
                else:
                    vendor = str(mr_dict.get("vendor") or "Vendor")
                    conf = mr_dict.get("confidence")
                    rec = mr_dict.get("recommendation")
                    title = f"HITL — 3-Way Match · {vendor}"
                    description = (
                        f"Confidence **{conf}%** · {rec or 'review'}. "
                        f"Approve to complete posting (UAT), or reject to cancel."
                    )
                    expanded = format_match_for_approval_detail(mr_dict)
                    agent_name = "Finance Agent"
            else:
                title = f"HITL — Confirm workflow `{r.workflow_key}`"
                description = (
                    "Automated steps finished. Approve to complete this run, "
                    "or reject to cancel (human-in-the-loop)."
                )
                expanded = (
                    (automation.get("message") or "")
                    + "\n\n"
                    + "No structured match payload — generic confirmation gate."
                ).strip()
                agent_name = "Workflow Kernel"

            appr = Approval(
                assignee_user_id=r.user_id,
                created_by_user_id=r.user_id,
                title=title,
                description=description,
                status=ApprovalStatus.PENDING.value,
                agent_name=agent_name,
                amount=None,
                confidence=int(mr_dict["confidence"])
                if mr_dict and mr_dict.get("confidence") is not None
                else None,
                risk="medium"
                if mr_dict and mr_dict.get("recommendation") == "human_review"
                else (
                    "medium"
                    if automation.get("handler") == "o2c_ohc_langgraph"
                    and len((automation.get("o2c_result") or {}).get("invoice_skips") or [])
                    else "low"
                ),
                payload={
                    "workflow_run_id": str(r.id),
                    "hitl_gate": "post_automation_confirmation",
                    "workflow_key": r.workflow_key,
                    "expanded_detail": expanded,
                    "match_result": mr_dict,
                    **(
                        {"o2c_result": automation.get("o2c_result")}
                        if automation.get("handler") == "o2c_ohc_langgraph"
                        else {}
                    ),
                },
            )
            db.add(appr)
            await db.flush()

            r.status = WorkflowRunStatus.AWAITING_HITL.value
            r.output_data = {
                "phase": "awaiting_human",
                "automation": automation,
                "message": "Pending your confirmation in Approvals.",
                "pending_approval_id": str(appr.id),
            }

            db.add(
                Notification(
                    user_id=r.user_id,
                    category="approval",
                    title=f"Workflow {r.workflow_key} needs your confirmation",
                    body="Open the review queue (O2C / S2P) to complete the human-in-the-loop step.",
                    link="/o2c/ohc-mis",
                )
            )

            await write_audit(
                db,
                actor_user_id=r.user_id,
                action="workflow.awaiting_hitl",
                resource_type="workflow_run",
                resource_id=str(r.id),
                details={"approval_id": str(appr.id), "handler": automation.get("handler")},
            )
            await db.commit()
    except Exception as e:
        log.exception("workflow %s failed", run_id)
        async with AsyncSessionLocal() as db:
            r = await db.get(WorkflowRun, run_id)
            if r:
                r.status = WorkflowRunStatus.FAILED.value
                r.error_message = str(e)[:2000]
                await db.commit()


@router.post("/trigger", response_model=dict)
async def trigger_workflow(
    body: TriggerBody,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    wk = body.workflow_key.strip()
    composable_def = None
    if settings.workflow_composable_enabled:
        composable_def = await fetch_workflow_definition(db, wk)
        if composable_def and composable_def.required_input_keys:
            req = composable_def.required_input_keys
            if not isinstance(req, list):
                req = []
            pl = body.payload or {}
            missing = [k for k in req if k not in pl or pl.get(k) in (None, "")]
            if missing:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    {
                        "detail": "Missing or empty required payload keys for this workflow",
                        "missing": missing,
                        "required_input_keys": req,
                    },
                )

    run = WorkflowRun(
        user_id=user.id,
        workflow_key=wk,
        status=WorkflowRunStatus.QUEUED.value,
        input_data=body.payload,
    )
    db.add(run)
    await db.flush()
    background_tasks.add_task(_execute_workflow_run, run.id)
    await write_audit(
        db,
        actor_user_id=user.id,
        action="workflow.trigger",
        resource_type="workflow_run",
        resource_id=str(run.id),
        details={"workflow_key": wk},
    )
    hint = ""
    compliance = wk.lower() in {k.lower() for k in COMPLIANCE_CALL_KEYS}
    if wk.lower() in {k.lower() for k in INVOICE_3WAY_KEYS}:
        hint = " Runs deterministic invoice 3-way match (mock SAP) before HITL."
    elif wk.lower() in {k.lower() for k in HR_CONTRACTOR_KEYS}:
        hint = " Runs deterministic contractor billing review (mock HRMS) before HITL."
    elif wk.lower() in {k.lower() for k in O2C_OHC_KEYS}:
        hint = (
            " Runs O2C OHC LangGraph (contract PDFs → agenos, attendance xlsx → invoices/MIS) before HITL."
        )
    elif composable_def:
        hint = " Runs composable pipeline from workflow_definitions (see GET /api/workflows/definitions)."
    base_msg = (
        "Runs compliance call quality batch (Drive → STT → rubric). "
        "This workflow key completes end-to-end without a separate HITL approval."
        if compliance
        else "After automation, a pending approval will complete this run."
    )
    return {
        "workflow_id": str(run.id),
        "status": "queued",
        "hitl": not compliance,
        "message": base_msg + hint,
    }


@router.get("", response_model=dict)
async def list_my_workflow_runs(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
):
    q = (
        select(WorkflowRun)
        .where(WorkflowRun.user_id == user.id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(min(limit, 100))
    )
    rows = (await db.execute(q)).scalars().all()
    return {
        "runs": [
            {
                "id": str(x.id),
                "workflow_key": x.workflow_key,
                "status": x.status,
                "created_at": x.created_at.isoformat(),
                "output": x.output_data,
            }
            for x in rows
        ]
    }


@router.get("/definitions", response_model=dict)
async def list_workflow_definitions(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_disabled: bool = False,
):
    """Catalog of composable pipelines (from `workflow_definitions`). Legacy keys are not listed unless seeded."""
    if include_disabled and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    q = select(WorkflowDefinition).order_by(WorkflowDefinition.workflow_key)
    if not include_disabled:
        q = q.where(WorkflowDefinition.enabled.is_(True))
    rows = (await db.execute(q)).scalars().all()
    return {
        "definitions": [
            {
                "workflow_key": r.workflow_key,
                "display_name": r.display_name,
                "description": r.description,
                "required_input_keys": r.required_input_keys or [],
                "version": r.version,
                "enabled": r.enabled,
                "step_count": len(r.steps) if isinstance(r.steps, list) else 0,
            }
            for r in rows
        ]
    }


@router.get("/definitions/by-key/{workflow_key}", response_model=dict)
async def get_workflow_definition_by_key(
    workflow_key: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    k = workflow_key.strip()
    r = await db.execute(select(WorkflowDefinition).where(WorkflowDefinition.workflow_key == k))
    row = r.scalar_one_or_none()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not row.enabled and not user.is_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return {
        "workflow_key": row.workflow_key,
        "display_name": row.display_name,
        "description": row.description,
        "required_input_keys": row.required_input_keys or [],
        "steps": row.steps,
        "version": row.version,
        "enabled": row.enabled,
    }


@router.get("/catalog", response_model=dict)
async def list_workflow_catalog_route(
    user: Annotated[User, Depends(get_current_user)],
):
    _ = user
    return {
        "workflows": list_workflow_catalog(),
        "source": "runtime_introspection",
    }


@router.get("/{workflow_id}/status", response_model=dict)
async def get_workflow_status(
    workflow_id: UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    r = await db.get(WorkflowRun, workflow_id)
    if not r or (
        not user.is_admin and r.user_id != user.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    progress = 0
    if r.status == WorkflowRunStatus.QUEUED.value:
        progress = 0
    elif r.status == WorkflowRunStatus.RUNNING.value:
        progress = 40
    elif r.status == WorkflowRunStatus.AWAITING_HITL.value:
        progress = 75
    elif r.status == WorkflowRunStatus.COMPLETED.value:
        progress = 100
    elif r.status in (
        WorkflowRunStatus.FAILED.value,
        WorkflowRunStatus.CANCELLED.value,
    ):
        progress = 0

    return {
        "workflow_id": str(r.id),
        "status": r.status,
        "progress": progress,
        "output": r.output_data,
        "error": r.error_message,
        "hitl": r.status == WorkflowRunStatus.AWAITING_HITL.value,
    }
