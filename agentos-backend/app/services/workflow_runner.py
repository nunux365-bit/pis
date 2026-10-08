"""Map workflow_key → automation phase (skills / graphs) for UAT and production."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.finance import run_finance_invoice_graph_async
from app.agents.o2c_ohc.agent import run_o2c_ohc_agent
from app.infra.thread_pools import run_cpu
from app.skills.hr.contractor_billing import run_contractor_billing_review

log = logging.getLogger(__name__)

# Keys that run the real invoice 3-way match (LangGraph + deterministic skill).
INVOICE_3WAY_KEYS = frozenset(
    {
        "invoice_3way_match",
        "finance.invoice_3way",
        "finance.invoice_3way_match",
        "demo_variance_check",  # Sessions UI — uses built-in skewed payload if none passed
    }
)

HR_CONTRACTOR_KEYS = frozenset(
    {
        "hr.contractor_billing",
        "skill.hr.contractor_billing",
        "hr.contractor_billing_review",
        "demo_contractor_billing",
    }
)

# O2C/OHC — contract PDF ingest (agenos) + shared-resource resolve + MIS drafts (DB + xlsx).
O2C_OHC_KEYS = frozenset(
    {
        "o2c_ohc",
        "o2c.ohc",
        "o2c_ohc_full",
        "demo_o2c_ohc",
    }
)

# Email automation — weekly Finance receivable reminder pipeline.
EMAIL_AUTOMATION_SCAN_KEYS = frozenset(
    {
        "email_automation_scan",
        "email_automation.scan",
        "email.automation.scan",
    }
)
EMAIL_AUTOMATION_DISPATCH_KEYS = frozenset(
    {
        "email_automation_dispatch",
        "email_automation.dispatch",
        "email.automation.dispatch",
    }
)

COMPLIANCE_CALL_KEYS = frozenset(
    {
        "compliance_call",
        "compliance.call",
        "call_quality",
        "glp_call_quality",
    }
)

OUTREACH_SYNC_KEYS = frozenset(
    {
        "outreach_sync",
        "outreach.sync",
        "cold_outreach_sync",
    }
)

OUTREACH_DISPATCH_KEYS = frozenset(
    {
        "outreach_dispatch",
        "outreach.dispatch",
        "cold_outreach_dispatch",
    }
)

OUTREACH_REPLIES_KEYS = frozenset(
    {
        "outreach_replies",
        "outreach.replies",
        "cold_outreach_replies",
    }
)

# Production / reporting: use this key in new integrations; aliases still work and normalize here.
CANONICAL_INVOICE_WORKFLOW_KEY = "invoice_3way_match"
CANONICAL_HR_CONTRACTOR_BILLING_KEY = "hr.contractor_billing"
CANONICAL_O2C_OHC_WORKFLOW_KEY = "o2c_ohc"
CANONICAL_EMAIL_AUTOMATION_SCAN_KEY = "email_automation_scan"
CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY = "email_automation_dispatch"
CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY = "compliance_call"
CANONICAL_OUTREACH_SYNC_KEY = "outreach_sync"
CANONICAL_OUTREACH_DISPATCH_KEY = "outreach_dispatch"
CANONICAL_OUTREACH_REPLIES_KEY = "outreach_replies"

# Workflows whose approved HITL may enqueue `post_hitl_outbox` (ERP / downstream handoff).
POST_HITL_ERP_CANONICAL_KEYS = frozenset(
    {
        CANONICAL_INVOICE_WORKFLOW_KEY,
        CANONICAL_HR_CONTRACTOR_BILLING_KEY,
    }
)


def canonical_workflow_key(submitted_key: str) -> str:
    """Normalize aliases → stable keys for ERP outbox and analytics."""
    k = (submitted_key or "").strip().lower()
    if k in {x.lower() for x in INVOICE_3WAY_KEYS}:
        return CANONICAL_INVOICE_WORKFLOW_KEY
    if k in {x.lower() for x in HR_CONTRACTOR_KEYS}:
        return CANONICAL_HR_CONTRACTOR_BILLING_KEY
    if k in {x.lower() for x in O2C_OHC_KEYS}:
        return CANONICAL_O2C_OHC_WORKFLOW_KEY
    if k in {x.lower() for x in EMAIL_AUTOMATION_SCAN_KEYS}:
        return CANONICAL_EMAIL_AUTOMATION_SCAN_KEY
    if k in {x.lower() for x in EMAIL_AUTOMATION_DISPATCH_KEYS}:
        return CANONICAL_EMAIL_AUTOMATION_DISPATCH_KEY
    if k in {x.lower() for x in COMPLIANCE_CALL_KEYS}:
        return CANONICAL_COMPLIANCE_CALL_WORKFLOW_KEY
    if k in {x.lower() for x in OUTREACH_SYNC_KEYS}:
        return CANONICAL_OUTREACH_SYNC_KEY
    if k in {x.lower() for x in OUTREACH_DISPATCH_KEYS}:
        return CANONICAL_OUTREACH_DISPATCH_KEY
    if k in {x.lower() for x in OUTREACH_REPLIES_KEYS}:
        return CANONICAL_OUTREACH_REPLIES_KEY
    return k or "unknown"


def workflow_eligible_for_post_hitl_erp(submitted_workflow_key: str) -> bool:
    """Gate SAP/ERP outbox rows — avoids enqueueing for arbitrary manual approvals."""
    return canonical_workflow_key(submitted_workflow_key) in POST_HITL_ERP_CANONICAL_KEYS


def _default_demo_variance_payload() -> dict[str, Any]:
    """Slight invoice vs PO variance for manual HITL practice."""
    return {
        "vendor": "MedPlus Logistics (UAT)",
        "invoice_number": "INV-DEMO-1042",
        "po_number": "4500999888",
        "invoice_total": "100500.00",
        "po_total": "100000.00",
        "grn_received_value": "100000.00",
        "taxable_value": "85169.49",
        "gst_rate_pct": "18",
    }


async def run_automation_phase(
    workflow_key: str,
    input_payload: dict | None,
    *,
    thread_id: str,
) -> dict[str, Any]:
    """
    Execute automation for this workflow_key.
    Returns a dict stored on WorkflowRun.output_data under key "automation".
    """
    key = (workflow_key or "").strip().lower()

    if key in {k.lower() for k in INVOICE_3WAY_KEYS}:
        payload = dict(input_payload or {})
        if key == "demo_variance_check" and not payload.get("invoice_total"):
            payload = {**_default_demo_variance_payload(), **payload}

        graph_out = await run_finance_invoice_graph_async(payload, thread_id=thread_id[:80])
        if graph_out.get("error"):
            log.warning("finance graph error: %s", graph_out["error"])
        return {
            "handler": "finance_langgraph_invoice_3way",
            "workflow_key": workflow_key,
            "graph": graph_out.get("graph"),
            "thread_id": graph_out.get("thread_id"),
            "match_result": graph_out.get("match_result"),
            "graph_error": graph_out.get("error"),
        }

    if key in {k.lower() for k in HR_CONTRACTOR_KEYS}:
        mr = run_contractor_billing_review(input_payload)
        return {
            "handler": "hr_contractor_billing_skill",
            "workflow_key": workflow_key,
            "match_result": mr,
        }

    if key in {k.lower() for k in O2C_OHC_KEYS}:
        graph_out = await run_cpu(
            lambda: run_o2c_ohc_agent(input_payload, thread_id=thread_id[:80]),
        )
        if graph_out.get("error"):
            log.warning("o2c_ohc agent reported errors: %s", graph_out["error"])
        o2c = graph_out.get("o2c_result")
        summary = ""
        if isinstance(o2c, dict):
            pr = o2c.get("pipeline_result") or {}
            inv = o2c.get("invoice_build_results") or []
            sk = o2c.get("invoice_skips") or []
            summary = (
                f"contracts_ok={pr.get('ok', 0)} failed={pr.get('failed', 0)} · "
                f"invoices={len(inv)} skipped_sites={len(sk)}"
            )
        return {
            "handler": "o2c_ohc_langgraph",
            "workflow_key": workflow_key,
            "graph": graph_out.get("graph"),
            "thread_id": graph_out.get("thread_id"),
            "o2c_result": o2c,
            "graph_error": graph_out.get("error"),
            "message": summary or "O2C OHC pipeline finished — see o2c_result in automation payload.",
        }

    if key in {k.lower() for k in EMAIL_AUTOMATION_SCAN_KEYS}:
        # Single unified email-automation graph in scan mode.
        from app.agents.email_automation import run_scan_async

        payload = dict(input_payload or {})
        max_results = int(payload.get("max_results") or 25)
        try:
            out = await run_scan_async(max_results=max_results)
        except Exception as e:  # pragma: no cover — surfaced in output
            log.exception("email_automation scan failed (thread=%s)", thread_id)
            return {
                "handler": "email_automation_langgraph",
                "workflow_key": workflow_key,
                "thread_id": thread_id,
                "graph_error": f"{type(e).__name__}: {e}",
            }
        result = out.get("result") or {}
        summary = (
            "disabled" if out.get("disabled")
            else f"ingested={result.get('message_count', 0)} "
                 f"queued_sends={result.get('send_count', 0)}"
        )
        return {
            "handler": "email_automation_langgraph",
            "workflow_key": workflow_key,
            "thread_id": thread_id,
            "graph": "email_automation.v1",
            "mode": "scan",
            "disabled": out.get("disabled", False),
            "result": result,
            "message": summary,
        }

    if key in {k.lower() for k in EMAIL_AUTOMATION_DISPATCH_KEYS}:
        from app.agents.email_automation import run_dispatch_async
        from app.email_automation.pipeline.dispatch import DEFAULT_DISPATCH_SEND_BATCH

        payload = dict(input_payload or {})
        limit = int(payload.get("limit") or DEFAULT_DISPATCH_SEND_BATCH)
        try:
            out = await run_dispatch_async(limit=limit)
        except Exception as e:  # pragma: no cover
            log.exception("email_automation dispatch failed (thread=%s)", thread_id)
            return {
                "handler": "email_automation_langgraph",
                "workflow_key": workflow_key,
                "thread_id": thread_id,
                "graph_error": f"{type(e).__name__}: {e}",
            }
        summary = (
            "disabled" if out.get("disabled")
            else (
                f"reclaim={out['reclaim'].get('requeued', 0)}/"
                f"{out['reclaim'].get('completed', 0)} "
                f"sent={len(out['sent_ids'])} failed={len(out['failed'])}"
            )
        )
        return {
            "handler": "email_automation_langgraph",
            "workflow_key": workflow_key,
            "thread_id": thread_id,
            "graph": "email_automation.v1",
            "mode": "dispatch",
            "disabled": out.get("disabled", False),
            "result": {
                "reclaim": out.get("reclaim"),
                "sent_ids": out.get("sent_ids"),
                "failed": out.get("failed"),
            },
            "message": summary,
        }

    if key in {k.lower() for k in COMPLIANCE_CALL_KEYS}:
        from app.agents.compliance_call.run import run_compliance_batch_async

        payload = dict(input_payload or {})
        mf = payload.get("max_files")
        try:
            out = await run_compliance_batch_async(
                max_files=int(mf) if mf is not None and str(mf).strip() != "" else None,
            )
        except Exception as e:  # pragma: no cover
            log.exception("compliance_call batch failed (thread=%s)", thread_id)
            return {
                "handler": "compliance_call_batch",
                "workflow_key": workflow_key,
                "thread_id": thread_id,
                "graph_error": f"{type(e).__name__}: {e}",
            }
        if out.get("graph_error"):
            return {
                "handler": "compliance_call_batch",
                "workflow_key": workflow_key,
                "thread_id": thread_id,
                "graph_error": out.get("graph_error"),
                "result": out,
            }
        summary = "disabled" if out.get("disabled") else (out.get("message") or "compliance_call batch")
        return {
            "handler": "compliance_call_batch",
            "workflow_key": workflow_key,
            "thread_id": thread_id,
            "graph": "compliance_call.v1",
            "disabled": out.get("disabled", False),
            "result": out,
            "message": summary,
        }

    # Generic placeholder — short path for unknown keys (extend with more handlers).
    return {
        "handler": "generic_sleep_stub",
        "workflow_key": workflow_key,
        "message": "No specialized automation registered for this key; HITL still applies.",
    }
