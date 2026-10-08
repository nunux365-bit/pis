"""
Per-``billing_profile`` rate_line normalizations at contract ingest (after JSON validation).

Shared attendance coercion stays in :mod:`ingest`; TACO/CWP invoice-level admin lives here.

**Scaling:** To add a new commercial family, extend :mod:`billing_profile` (new profile value +
``infer_billing_profile``), add a branch in ``apply_profile_rate_line_normalization``, and keep
logic in a dedicated function (same pattern as TACO). For one-off client slugs, a future
``INGEST_RATE_LINE_NORMALIZERS_BY_SLUG`` registry can call optional hooks after profile strategies
without changing core ingest.
"""

from __future__ import annotations

from typing import Any, Callable

# Optional future hook: ``slug -> callable(rates, extraction_metadata) -> bool`` after profile strategies.
INGEST_RATE_LINE_NORMALIZERS_BY_SLUG: dict[str, Callable[..., bool]] = {}

from app.agents.o2c_ohc.billing_constants import (
    INVOICE_ADMIN_PCT_BILLING_RULE_KEY,
    OHC_INVOICE_ADMIN_ROLE_CODE,
    SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE,
)
from app.agents.o2c_ohc.billing_profile import BILLING_PROFILE_TACO, normalize_billing_profile
from app.agents.o2c_ohc.llm_extract import CONTRACT_PROMPT_PROFILE_TACO, normalize_contract_prompt_profile

_TACO_DEFAULT_INVOICE_ADMIN_PCT = 10.0


def taco_invoice_admin_normalization_applies(
    *,
    billing_profile: str | None,
    extraction_metadata: dict[str, Any],
) -> bool:
    if billing_profile is not None and normalize_billing_profile(billing_profile) == BILLING_PROFILE_TACO:
        return True
    em_bp = extraction_metadata.get("billing_profile")
    if em_bp is not None and str(em_bp).strip():
        if normalize_billing_profile(str(em_bp)) == BILLING_PROFILE_TACO:
            return True
    return normalize_contract_prompt_profile(extraction_metadata.get("contract_prompt_profile")) == CONTRACT_PROMPT_PROFILE_TACO


def _effective_percentage_service_charge(rl: dict[str, Any]) -> float | None:
    """Positive per-line percentage SC (legacy); stripped for TACO invoice-level admin."""
    raw_type = str(rl.get("service_charge_type") or "").strip().lower()
    if raw_type in ("none", "", "null", "-"):
        raw_type = ""
    sv = rl.get("service_charge_value")
    try:
        v = float(sv) if sv is not None and sv != "" else 0.0
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return None
    if "percent" in raw_type or raw_type in ("pct", "percentage", "%"):
        return round(v, 6)
    return None


def normalize_taco_invoice_level_admin_lines(
    rates: list[dict[str, Any]],
    *,
    extraction_metadata: dict[str, Any],
    billing_profile: str | None = None,
) -> bool:
    """
    TACO / CWP: model administration as **% of monthly staffing subtotal** via one
    ``OHC_ADMIN_INVOICE_PCT`` line per ``site_key``; clear per-line ``service_charge_*`` on staffing.

    No-op when neither persisted ``billing_profile`` nor extract profile indicates TACO.
    Pass ``billing_profile`` from ingest when metadata may omit it (tests may only set prompt profile).
    """
    if not taco_invoice_admin_normalization_applies(
        billing_profile=billing_profile, extraction_metadata=extraction_metadata
    ):
        return False

    changed = False
    before = len(rates)
    rates[:] = [
        rl
        for rl in rates
        if not (
            isinstance(rl, dict)
            and str(rl.get("role_code") or "").strip().upper() == OHC_INVOICE_ADMIN_ROLE_CODE
        )
    ]
    if len(rates) != before:
        changed = True

    by_site: dict[str | None, list[dict[str, Any]]] = {}
    for rl in rates:
        if not isinstance(rl, dict):
            continue
        if str(rl.get("billing_model") or "").strip() != "rate_attendance":
            continue
        sk = rl.get("site_key")
        key: str | None = str(sk).strip() if sk is not None and str(sk).strip() else None
        by_site.setdefault(key, []).append(rl)

    for site_key, staff in by_site.items():
        if not staff:
            continue
        pcts = [_effective_percentage_service_charge(rl) for rl in staff]
        pos = sorted({p for p in pcts if p is not None})
        for rl in staff:
            if str(rl.get("service_charge_type") or "").strip().lower() not in ("none", "", "null"):
                changed = True
            rl["service_charge_type"] = "none"
            rl["service_charge_value"] = None

        if len(pos) > 1:
            continue
        pct = float(pos[0]) if len(pos) == 1 else _TACO_DEFAULT_INVOICE_ADMIN_PCT

        rates.append(
            {
                "site_key": site_key,
                "billing_model": "fixed_monthly",
                "role_code": OHC_INVOICE_ADMIN_ROLE_CODE,
                "description": "Administration charge (% of monthly staffing subtotal per contract)",
                "rate_amount": 0.0,
                "rate_unit": "month",
                "contracted_quantity": 1,
                "attendance_required": False,
                "minimum_units_per_period": None,
                "unfilled_penalty_pct": 0,
                "ot_multiplier": None,
                "service_charge_type": "none",
                "service_charge_value": None,
                "actuals_markup_pct": None,
                "schedule_type": "none",
                "schedule_config": {},
                "billing_rules": {
                    "deduction": None,
                    "pro_rata": None,
                    INVOICE_ADMIN_PCT_BILLING_RULE_KEY: pct,
                },
                "billing_rule_text": (
                    f"Contract administration: {pct}% of the sum of OHC staffing (rate_attendance) "
                    "amounts for this site for the billing period (invoice-level admin — not per annexure row)."
                ),
                "source_ref": {"note": SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE},
            }
        )
        changed = True

    return changed


def apply_profile_rate_line_normalization(
    rates: list[dict[str, Any]],
    *,
    billing_profile: str,
    extraction_metadata: dict[str, Any],
) -> bool:
    """
    Run profile-specific mutating normalizers on ``rate_lines`` (in place).

    Returns True if any normalizer reported changes.
    """
    changed = False
    if taco_invoice_admin_normalization_applies(
        billing_profile=billing_profile, extraction_metadata=extraction_metadata
    ):
        if normalize_taco_invoice_level_admin_lines(
            rates,
            extraction_metadata=extraction_metadata,
            billing_profile=billing_profile,
        ):
            changed = True
    return changed
