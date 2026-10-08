"""Contractor timesheet → billing draft — deterministic (no LLM).

UAT stand-in for Darwinbox / vendor master reads. Real HRMS plugs in at the same seams.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def _money(v: Any, default: str = "0") -> Decimal:
    if v is None or v == "":
        return Decimal(default)
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def run_contractor_billing_review(input_payload: dict | None) -> dict[str, Any]:
    """
    Validate hours × rate vs claimed amount; flag policy outliers for HITL.

    Optional payload keys:
    - contractor_name, engagement_id, period_label
    - hours_worked, hourly_rate_inr, claimed_amount_inr
    - max_hours_per_week (default 48) — trips human_review if exceeded
    """
    p = dict(input_payload or {})

    name = str(p.get("contractor_name") or "Demo Contractor")
    engagement = str(p.get("engagement_id") or "ENG-UAT-001")
    period = str(p.get("period_label") or "2026-03")

    hours = _money(p.get("hours_worked"), "160")
    rate = _money(p.get("hourly_rate_inr"), "850.00")
    claimed = _money(p.get("claimed_amount_inr"), str(hours * rate))
    cap_hours = _money(p.get("max_hours_per_week"), "48") * Decimal("4")  # ~4 weeks

    expected = (hours * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    delta = claimed - expected
    delta_ok = abs(delta) <= Decimal("2.00")

    discrepancies: list[dict[str, str]] = []
    if not delta_ok:
        discrepancies.append(
            {
                "field": "amount_vs_computed",
                "detail": f"Claimed ₹{claimed} vs hours×rate ₹{expected} (Δ {delta:+.2f})",
            }
        )
    if hours > cap_hours:
        discrepancies.append(
            {
                "field": "hours_cap",
                "detail": f"Hours {hours} exceed policy cap {cap_hours} for the period",
            }
        )

    confidence = 96
    confidence -= min(35, int(abs(delta)))
    if hours > cap_hours:
        confidence -= 20
    confidence = max(0, min(100, confidence))

    if not discrepancies and delta_ok:
        recommendation = "auto_approve_candidate"
    else:
        recommendation = "human_review"

    matched = not discrepancies and delta_ok

    return {
        "skill": "hr.contractor_billing",
        "version": "1.0.0",
        "deterministic": True,
        "hrms_mode": "mock_darwinbox_readonly",
        "contractor_name": name,
        "engagement_id": engagement,
        "period_label": period,
        "hours_worked": str(hours),
        "hourly_rate_inr": str(rate),
        "claimed_amount_inr": str(claimed),
        "expected_amount_inr": str(expected),
        "amount_matches_computed": delta_ok,
        "policy_hours_cap": str(cap_hours),
        "discrepancies": discrepancies,
        "matched": matched,
        "confidence": confidence,
        "recommendation": recommendation,
    }


def format_contractor_billing_detail(result: dict[str, Any]) -> str:
    lines = [
        f"Contractor: {result.get('contractor_name')} ({result.get('engagement_id')})",
        f"Period: {result.get('period_label')}",
        f"Hours: {result.get('hours_worked')} × ₹{result.get('hourly_rate_inr')}/hr",
        f"Claimed: ₹{result.get('claimed_amount_inr')} · Expected: ₹{result.get('expected_amount_inr')}",
        f"Amount OK: {result.get('amount_matches_computed')} · Confidence: {result.get('confidence')}%",
        f"Recommendation: {result.get('recommendation')}",
    ]
    disc = result.get("discrepancies") or []
    if disc:
        lines.append("Flags:")
        for d in disc:
            lines.append(f"  • [{d.get('field')}] {d.get('detail')}")
    return "\n".join(lines)
