"""Invoice 3-Way Match — deterministic core (no LLM).

Steps 1–5: structured payload + mock PO/GRN (read-only ERP stand-in for UAT).
Real SAP/GST services plug in later at the same seams.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def _money(v: Any, default: str = "0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pct_diff(a: Decimal, b: Decimal) -> float:
    if b == 0:
        return 0.0 if a == 0 else 100.0
    return float(((a - b) / b * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def run_invoice_3way_match(input_payload: dict | None) -> dict[str, Any]:
    """
    Deterministic 3-way match against mock PO + GRN embedded in or derived from payload.

    Expected payload keys (all optional):
    - vendor, invoice_number, po_number
    - invoice_total, taxable_value, gst_rate_pct (default 18)
    - po_total, grn_received_value  (default: align with invoice for "clean" runs)
    - force_gst_fail: bool — test harness only
    """
    p = dict(input_payload or {})

    vendor = str(p.get("vendor") or "Demo Vendor Pvt Ltd")
    invoice_number = str(p.get("invoice_number") or "INV-UAT-001")
    po_number = str(p.get("po_number") or "4500123456")

    invoice_total = _money(p.get("invoice_total"), "125000.00")
    taxable = _money(p.get("taxable_value"), str(invoice_total / Decimal("1.18")))
    gst_rate = _money(p.get("gst_rate_pct"), "18")
    po_total = _money(p.get("po_total"), str(invoice_total))
    grn_val = _money(p.get("grn_received_value"), str(po_total))

    # Mock SAP read — values are deterministic from payload (no network).
    po_line = {
        "po_number": po_number,
        "vendor": vendor,
        "total": str(po_total),
        "currency": str(p.get("currency") or "INR"),
        "source": "mock_sap_po_read",
    }
    grn_line = {
        "po_number": po_number,
        "received_value": str(grn_val),
        "source": "mock_sap_grn_read",
    }

    variance_inv_po = _pct_diff(invoice_total, po_total)
    variance_po_grn = _pct_diff(grn_val, po_total)

    expected_gst = (taxable * gst_rate / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    implied_total = (taxable + expected_gst).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    gst_ok = bool(p.get("force_gst_fail")) is False and abs(invoice_total - implied_total) <= Decimal(
        "2.00"
    )

    discrepancies: list[dict[str, str]] = []
    if abs(variance_inv_po) >= 0.01:
        discrepancies.append(
            {
                "field": "invoice_vs_po",
                "detail": f"Invoice total {invoice_total} vs PO {po_total} ({variance_inv_po:+.4f}%)",
            }
        )
    if abs(variance_po_grn) >= 0.01:
        discrepancies.append(
            {
                "field": "grn_vs_po",
                "detail": f"GRN value {grn_val} vs PO {po_total} ({variance_po_grn:+.4f}%)",
            }
        )
    if not gst_ok:
        discrepancies.append(
            {
                "field": "gst",
                "detail": f"GST check failed: taxable {taxable} @ {gst_rate}% → expect ~{implied_total}, got {invoice_total}",
            }
        )

    # Confidence: start high, penalize variance and GST issues
    confidence = 97
    confidence -= min(40, int(abs(variance_inv_po) * 8))
    confidence -= min(30, int(abs(variance_po_grn) * 8))
    if not gst_ok:
        confidence -= 25
    confidence = max(0, min(100, confidence))

    if not discrepancies and gst_ok:
        recommendation = "auto_approve_candidate"
    elif not gst_ok or abs(variance_inv_po) > 1.0 or abs(variance_po_grn) > 1.0:
        recommendation = "human_review"
    else:
        recommendation = "human_review"

    matched = not discrepancies and gst_ok

    return {
        "skill": "invoice_3way_match",
        "version": "1.0.0",
        "deterministic": True,
        "erp_mode": "mock_sap_readonly",
        "vendor": vendor,
        "invoice_number": invoice_number,
        "invoice_total": str(invoice_total),
        "taxable_value": str(taxable),
        "gst_rate_pct": str(gst_rate),
        "gst_computation_ok": gst_ok,
        "po": po_line,
        "grn": grn_line,
        "variance_invoice_vs_po_pct": variance_inv_po,
        "variance_grn_vs_po_pct": variance_po_grn,
        "discrepancies": discrepancies,
        "matched": matched,
        "confidence": confidence,
        "recommendation": recommendation,
    }


def format_match_for_approval_detail(result: dict[str, Any]) -> str:
    """Human-readable block for approval expanded_detail / chat."""
    lines = [
        f"Vendor: {result.get('vendor')}",
        f"Invoice: {result.get('invoice_number')} — ₹{result.get('invoice_total')}",
        f"PO {result.get('po', {}).get('po_number')}: ₹{result.get('po', {}).get('total')}",
        f"GRN received: ₹{result.get('grn', {}).get('received_value')}",
        f"Variance inv↔PO: {result.get('variance_invoice_vs_po_pct'):+.4f}%",
        f"GST OK: {result.get('gst_computation_ok')} · Confidence: {result.get('confidence')}%",
        f"Recommendation: {result.get('recommendation')}",
    ]
    disc = result.get("discrepancies") or []
    if disc:
        lines.append("Flags:")
        for d in disc:
            lines.append(f"  • [{d.get('field')}] {d.get('detail')}")
    return "\n".join(lines)
