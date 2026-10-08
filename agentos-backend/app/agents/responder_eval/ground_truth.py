"""Build compact eval_ground_truth from RCA run document."""

from __future__ import annotations

from typing import Any

_PAYMENT_KEYS = (
    "mode", "amount_payable", "online_refund_due", "total_refund_due",
    "online_refund_initiated", "total_refunded_amount", "allow_payment_retry",
    "online_collected", "online_pending_amount",
)
_SHIPMENT_KEYS = (
    "delivery_date", "tracking_number", "refund_date", "delivery_retries", "status",
)


def _pick(d: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any]:
    if not d:
        return {}
    return {k: d[k] for k in keys if k in d}


def _condense_preflight(pf: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in pf.items() if k != "promised_delivery_raw"}


def _condense_operations(ops: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ops.get(k)
        for k in (
            "status_chronology",
            "status_transitions",
            "shipping_summary",
            "last_mile_mode",
            "split_child",
            "parent_id",
            "order_id",
            "timeline_note",
            "history_count",
            "groot_events",
            "segments",
            "clickpost_events",
            "return_followed",
            "return_note",
        )
        if k in ops
    }


def _condense_cart(caj: dict[str, Any]) -> dict[str, Any]:
    return {k: caj.get(k) for k in ("headline", "insights", "meaningful_changes", "cart_vs_order", "note") if k in caj}


def _condense_p1(p1: dict[str, Any]) -> dict[str, Any]:
    if not p1:
        return {}
    status = p1.get("status")
    msn = p1.get("msn_adherence") or {}
    if status and status != "pending_data":
        return p1
    return {
        "status": status,
        "msn_adherence": {
            k: msn.get(k)
            for k in ("status", "headline", "summary", "breach_count")
            if k in msn
        },
    }


def _order_ops_slice(report: dict[str, Any]) -> dict[str, Any]:
    od = report.get("order_details") or {}
    return {
        "payment_summary": _pick(od.get("payment_summary") or {}, _PAYMENT_KEYS),
        "shipment_detail": _pick(od.get("shipment_detail") or {}, _SHIPMENT_KEYS),
    }


def build_eval_artifact(run_doc: dict[str, Any]) -> dict[str, Any]:
    report = run_doc.get("report") or {}
    facts = report.get("facts") or {}
    pf = facts.get("preflight") or {}
    po = facts.get("perfect_order") or {}

    artifact: dict[str, Any] = {
        "run_id": run_doc.get("run_id"),
        "order_id": run_doc.get("order_id"),
        "status": run_doc.get("status"),
        "schema_version": facts.get("schema_version"),
        "warnings": facts.get("warnings"),
        "perfect_order": po,
        "sku_price_increases": facts.get("sku_price_increases") or {
            "total_increase": 0.0,
            "count": 0,
            "source": "none",
            "lines": [],
        },
        "sku_price_decreases": facts.get("sku_price_decreases") or {
            "total_decrease": 0.0,
            "count": 0,
            "source": "none",
            "lines": [],
        },
        "post_order_sku_removals": facts.get("post_order_sku_removals") or {
            "available": False,
            "count": 0,
            "items": [],
            "ambiguous_not_on_order": [],
        },
        "preflight": _condense_preflight(pf),
        "skus": facts.get("skus"),
        "cart_allocation_journey": _condense_cart(facts.get("cart_allocation_journey") or {}),
        "p1": _condense_p1(facts.get("p1") or {}),
        "p2": facts.get("p2"),
        "split_insights": facts.get("split_insights"),
        "operations": _condense_operations(facts.get("operations") or {}),
        "signals": facts.get("signals"),
        "allocation_unavailable": facts.get("allocation_unavailable"),
        "order_ops": _order_ops_slice(report),
    }
    artifact["rca_verdict"] = (
        f"status={pf.get('order_status')}; eta_breached={pf.get('is_eta_breached')}; "
        f"perfect_order={'pass' if po.get('overall_pass') else 'fail'}; hint={po.get('hint') or '—'}"
    )
    return artifact
