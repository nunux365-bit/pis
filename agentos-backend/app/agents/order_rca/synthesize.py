"""OpenAI RCA synthesis (lean context in, narrative out)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agents.order_rca.llm_context import build_llm_context
from app.infra.openai_async_client import get_shared_openai_client
from app.agents.order_rca.constants import DELIVERY_BREACH_DELIVERED_LATE, DELIVERY_BREACH_OPEN_PAST
from app.agents.order_rca import rules
from app.agents.order_rca.synthesis_prompt import ORDER_RCA_SYNTHESIS_SYSTEM_PROMPT
from app.agents.order_rca.time_utils import format_duration_minutes
from app.config.settings import settings

log = logging.getLogger(__name__)

HYPOTHESIS_DISPLAY_LIMIT = 10


def empty_synthesis() -> dict[str, Any]:
    """Blank narrative shape for internal RCA runs (no OpenAI / templates)."""
    return {
        "verdict": "",
        "verdict_subline": "",
        "primary_cause": "",
        "contributing_factors": [],
        "recommended_action": "",
        "segment_notes": [],
        "data_gaps": [],
        "hypotheses": [],
    }


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string"},
        "verdict_subline": {"type": "string"},
        "primary_cause": {"type": "string"},
        "contributing_factors": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string"},
        "segment_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["label", "note"],
            },
        },
        "data_gaps": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {
            "type": "array",
            "maxItems": HYPOTHESIS_DISPLAY_LIMIT,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "finding": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "alignment": {
                        "type": "string",
                        "enum": ["supported", "partial", "contradicted", "unknown"],
                    },
                },
                "required": ["finding", "hypothesis", "alignment"],
            },
        },
    },
    "required": [
        "verdict",
        "verdict_subline",
        "primary_cause",
        "contributing_factors",
        "recommended_action",
        "segment_notes",
        "data_gaps",
        "hypotheses",
    ],
}


_UNSPLIT_PARENT_PHRASE = re.compile(r"\bparent order\b", re.IGNORECASE)
_UNSPLIT_CHILD_PHRASE = re.compile(r"\bchild order\b", re.IGNORECASE)
_RAW_MINUTES_PHRASE = re.compile(r"\b(\d{3,})\s*(?:minutes?|mins?)\b", re.IGNORECASE)
_ALLOC_SUCCEEDED_BUT = re.compile(
    r"^(?:preferred\s+)?(?:warehouse\s+)?allocation\s+succeeded,?\s*but\b",
    re.IGNORECASE,
)


def _humanize_raw_minutes_in_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        fmt = format_duration_minutes(int(match.group(1)))
        return fmt if fmt else match.group(0)

    return _RAW_MINUTES_PHRASE.sub(repl, text)


def _sanitize_synthesis_narrative(ctx: dict[str, Any], syn: dict[str, Any]) -> dict[str, Any]:
    """Guardrails when the model ignores split-order, duration, or imperfect-order rules."""

    def _fix(text: str) -> str:
        out = text
        if not ctx.get("is_split_order"):
            out = _UNSPLIT_PARENT_PHRASE.sub("The order", out)
            out = _UNSPLIT_CHILD_PHRASE.sub("The order", out)
        out = _humanize_raw_minutes_in_text(out)
        if _ALLOC_SUCCEEDED_BUT.search(out):
            failed = (ctx.get("perfect_order_summary") or {}).get("failed_pillars") or []
            lead = failed[0] if failed else {}
            detail = str(lead.get("detail") or lead.get("label") or "").strip()
            delivery_eta = (ctx.get("order_summary") or {}).get("delivery_eta") or {}
            headline = _delivery_failure_headline(lead, delivery_eta)
            if headline:
                out = headline
            elif detail:
                out = detail
        return out

    for key in ("verdict", "verdict_subline", "primary_cause", "recommended_action"):
        val = syn.get(key)
        if isinstance(val, str) and val:
            syn[key] = _fix(val)
    syn["contributing_factors"] = [
        _fix(f) for f in (syn.get("contributing_factors") or []) if isinstance(f, str)
    ]
    for row in syn.get("hypotheses") or []:
        if not isinstance(row, dict):
            continue
        for key in ("finding", "hypothesis"):
            val = row.get(key)
            if isinstance(val, str) and val:
                row[key] = _fix(val)
    return syn


def _failed_pillars(po: dict[str, Any]) -> list[dict[str, Any]]:
    return rules.rank_failed_pillars(list(po.get("pillars") or []))


def _delivery_failure_headline(lead: dict[str, Any], delivery_eta: dict[str, Any]) -> str | None:
    if str(lead.get("id") or "") != "delivery":
        return None
    breach = delivery_eta.get("breach_minutes_display")
    kind = delivery_eta.get("breach_kind")
    if kind == DELIVERY_BREACH_DELIVERED_LATE and breach:
        return f"Delivered {breach} late vs first promise"
    if kind == DELIVERY_BREACH_OPEN_PAST and breach:
        return f"{breach} past first promise; not delivered"
    detail = lead.get("detail") or lead.get("label")
    return str(detail).strip() if detail else None


def _imperfect_verdict(failed: list[dict[str, Any]], delivery_eta: dict[str, Any]) -> str:
    lead = failed[0]
    headline = _delivery_failure_headline(lead, delivery_eta)
    if headline:
        return headline
    pid = str(lead.get("id") or "")
    label = str(lead.get("label") or pid.replace("_", " ").title())
    detail = str(lead.get("detail") or "").strip()
    return detail if detail else f"{label} check failed"


def _imperfect_primary_cause(
    failed: list[dict[str, Any]],
    delivery_eta: dict[str, Any],
    order_summary: dict[str, Any],
    seeds: list[dict[str, str]],
) -> str:
    lead = failed[0]
    pid = str(lead.get("id") or "")
    if pid == "delivery":
        promised = delivery_eta.get("promised_first")
        actual = order_summary.get("actual_delivery")
        breach = delivery_eta.get("breach_minutes_display")
        kind = delivery_eta.get("breach_kind")
        if kind == DELIVERY_BREACH_DELIVERED_LATE and promised and actual and breach:
            base = f"Delivery was {breach} late vs first promise ({promised} → {actual})."
        elif kind == DELIVERY_BREACH_OPEN_PAST and breach:
            base = str(lead.get("detail") or f"{breach} past first promise; not delivered.")
        else:
            base = str(lead.get("detail") or "Delivery pillar failed.")
    else:
        base = str(lead.get("detail") or lead.get("label") or "See failed pillar.")
    ops_seed = next((s for s in seeds if str(s.get("id", "")).startswith("ops_")), None)
    if ops_seed and ops_seed.get("finding_hint"):
        return f"{base} Ops: {ops_seed['finding_hint']}."
    return base


def _recommended_action_for_failures(failed: list[dict[str, Any]], *, ctx: dict[str, Any] | None = None) -> str:
    ids = {str(p.get("id") or "") for p in failed}
    if "delivery" in ids:
        mode = ((ctx or {}).get("operations_summary") or {}).get("last_mile_mode") or "none"
        if mode == "clickpost":
            return "Review the operations trace and ClickPost courier scans for where fulfilment ran late."
        if mode == "groot":
            return "Review the operations trace and Groot timeline for where fulfilment ran late."
        return "Review the operations trace and shipping details for where fulfilment ran late."
    if "allocation" in ids:
        return "Open the allocation store matrix and compare why nearer stores were not used."
    if "price_integrity" in ids:
        return "Review line-level MRP increases vs cart on the order lines."
    if "pushback" in ids:
        return "Check status history for allocation regression after fulfilment started."
    return "Review the failed pillar details in the scorecard below."


def mock_synthesis(ctx: dict[str, Any]) -> dict[str, Any]:
    alloc = ctx.get("allocation_summary") or {}
    badge = alloc.get("badge") or "UNKNOWN"
    delivery_eta = (ctx.get("order_summary") or {}).get("delivery_eta") or {}
    po = ctx.get("perfect_order_summary") or {}
    po_overall = po.get("overall")
    failed = _failed_pillars(po)
    gaps = []
    unavail = ctx.get("allocation_unavailable")
    if isinstance(unavail, dict) and unavail.get("message"):
        gaps.append(str(unavail["message"]))
    pg = ctx.get("planning_gaps") or {}
    if pg.get("p1") in ("pending_api", "pending_data"):
        gaps.append("MSN adherence needs P1 planning API (MSN, on-shelf, SKU sub grade)")
    if pg.get("p2") == "not_configured":
        gaps.append("Procurement funnel data not connected")
    segments = (ctx.get("operations_summary") or {}).get("segments") or []
    if badge == "N/A" and unavail:
        verdict = "Allocation data not available for this order"
        subline = ""
        primary = str(unavail.get("message") or "Allocation Data not available for this Order.")
        action = "Use operations timeline and order status; allocation snapshot was not returned for this PO."
    elif po_overall == "perfect":
        verdict = "Perfect order — all checks passed"
        subline = ""
        primary = "All evaluated Perfect Order pillars passed."
        action = "No further RCA action required."
    elif failed:
        order_summary = ctx.get("order_summary") or {}
        seeds = ctx.get("hypothesis_seeds") or []
        verdict = _imperfect_verdict(failed, delivery_eta)
        actual = order_summary.get("actual_delivery")
        promised = delivery_eta.get("promised_first")
        breach_disp = delivery_eta.get("breach_minutes_display")
        kind = delivery_eta.get("breach_kind")
        if kind == DELIVERY_BREACH_DELIVERED_LATE and actual and promised and breach_disp:
            subline = f"Promised {promised}; delivered {actual}."
        else:
            subline = str(failed[0].get("detail") or "")
        primary = _imperfect_primary_cause(failed, delivery_eta, order_summary, seeds)
        action = _recommended_action_for_failures(failed, ctx=ctx)
    else:
        verdict = "Imperfect order — review scorecard"
        subline = ""
        primary = "See Perfect Order pillar scorecard for failed checks."
        action = "Review the failed pillar details in the scorecard below."
    return {
        "verdict": verdict,
        "verdict_subline": subline,
        "primary_cause": primary,
        "contributing_factors": list(ctx.get("signals") or [])[:5],
        "recommended_action": action,
        "segment_notes": [
            {
                "label": s.get("label", "Segment"),
                "note": (
                    f"Took {format_duration_minutes(s.get('duration_min')) or s.get('duration_display')}."
                    if (s.get("duration_min") is not None or s.get("duration_display"))
                    else f"{s.get('label', 'Segment')} phase."
                ),
            }
            for s in segments
            if s.get("label")
        ],
        "data_gaps": gaps,
        "hypotheses": [
            {
                "finding": f"Allocation badge is {badge}",
                "hypothesis": "A non-preferred store may add distance when badge is CROSS",
                "alignment": "supported" if badge == "CROSS" else "partial",
            }
        ],
    }


def _planning_data_gaps(facts: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    unavail = facts.get("allocation_unavailable")
    if isinstance(unavail, dict) and unavail.get("message"):
        gaps.append(str(unavail["message"]))
    p1 = facts.get("p1") or {}
    msn = p1.get("msn_adherence") or {}
    if msn.get("status") in ("pending_api", "pending_data"):
        gaps.append("MSN, on-shelf, and SKU sub grade need P1 planning API")
    p2 = facts.get("p2") or {}
    if p2.get("status") == "not_configured":
        gaps.append("Procurement funnel data not connected")
    return gaps


def allocation_unavailable_synthesis(facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic stub when allocation snapshot is missing — no OpenAI call."""
    unavail = facts.get("allocation_unavailable") or {}
    msg = str(unavail.get("message") or "Allocation Data not available for this Order.")
    return {
        "verdict": msg,
        "verdict_subline": "",
        "primary_cause": msg,
        "contributing_factors": [],
        "recommended_action": "",
        "segment_notes": [],
        "data_gaps": [],
        "hypotheses": [],
    }


def perfect_order_synthesis(facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic narrative for Perfect Order — no OpenAI call, no hypotheses."""
    verdict = "Perfect order — all checks passed"
    subline = ""
    primary = "All evaluated Perfect Order pillars passed."
    action = "No further RCA action required."

    return {
        "verdict": verdict,
        "verdict_subline": subline,
        "primary_cause": primary,
        "contributing_factors": list(facts.get("signals") or [])[:5],
        "recommended_action": action,
        "segment_notes": [],
        "data_gaps": _planning_data_gaps(facts),
        "hypotheses": [],
    }


async def synthesize_rca(facts: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if facts.get("allocation_unavailable"):
        return allocation_unavailable_synthesis(facts), "allocation_unavailable_template"

    po = facts.get("perfect_order") or {}
    if po.get("overall_pass") is True:
        return perfect_order_synthesis(facts), "perfect_template"

    ctx = build_llm_context(facts)
    if settings.order_rca_mock_llm or not (settings.openai_api_key or "").strip():
        return _sanitize_synthesis_narrative(ctx, mock_synthesis(ctx)), "mock"

    model = (settings.order_rca_openai_model or settings.openai_chat_model or "").strip()
    user_content = (
        "Write the Order RCA JSON report for the facts below.\n"
        "If perfect_order_summary.overall_pass is false: headline failed_pillars first; "
        "never parent/child unless is_split_order; human-readable durations only.\n"
        "Rewrite hypothesis_seeds into grammatical finding and hypothesis pairs.\n\n"
        f"{json.dumps(ctx, default=str)}"
    )
    try:
        temperature = float(settings.order_rca_synthesis_temperature)
        client = get_shared_openai_client()
        resp = await client.responses.create(
            model=model,
            temperature=temperature,
            input=[
                {"role": "system", "content": ORDER_RCA_SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "order_rca",
                    "strict": True,
                    "schema": _SCHEMA,
                }
            },
        )
        for item in resp.output or []:
            if getattr(item, "type", None) == "message":
                for c in item.content or []:
                    if getattr(c, "type", None) == "output_text":
                        syn = json.loads(c.text)
                        return _sanitize_synthesis_narrative(ctx, syn), "openai"
    except Exception as e:
        log.warning("OpenAI RCA failed, using mock: %s", e)
    return _sanitize_synthesis_narrative(ctx, mock_synthesis(ctx)), "fallback"
