"""Validate approved-style MIS summary JSON shapes for golden / regression tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def validate_mis_summary_shape(obj: Any) -> list[str]:
    """Return a list of human-readable errors; empty means the object is structurally acceptable."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["root must be a JSON object"]

    for k in ("mis_summary", "summary_rows", "totals", "validation"):
        if k not in obj:
            errs.append(f"missing_top_level_key:{k}")

    ms = obj.get("mis_summary")
    if ms is not None:
        if not isinstance(ms, dict):
            errs.append("mis_summary must be an object")
        else:
            for fld in ("client_site_key", "billing_period_start", "billing_period_end", "working_days"):
                if fld not in ms:
                    errs.append(f"mis_summary.missing:{fld}")

    rows = obj.get("summary_rows")
    if not isinstance(rows, list):
        errs.append("summary_rows must be a list")
    else:
        required_row = (
            "contract_rate_line_id",
            "service",
            "final_amount",
            "is_omitted",
            "present_days",
            "absent_days",
        )
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                errs.append(f"summary_rows[{i}] must be an object")
                continue
            for fld in required_row:
                if fld not in r:
                    errs.append(f"summary_rows[{i}].missing:{fld}")

    tot = obj.get("totals")
    if tot is not None:
        if not isinstance(tot, dict):
            errs.append("totals must be an object")
        else:
            for fld in ("final_amount_total", "rows_included", "rows_omitted"):
                if fld not in tot:
                    errs.append(f"totals.missing:{fld}")

    val = obj.get("validation")
    if val is not None:
        if not isinstance(val, dict):
            errs.append("validation must be an object")
        else:
            for fld in ("status", "validation_errors", "self_checks"):
                if fld not in val:
                    errs.append(f"validation.missing:{fld}")

    return errs


def mis_totals_consistent(obj: dict[str, Any], *, tol: Decimal = Decimal("0.02")) -> list[str]:
    """Check totals.final_amount_total ~= sum(non-omitted final_amount)."""
    errs: list[str] = []
    rows = obj.get("summary_rows")
    tot = obj.get("totals")
    if not isinstance(rows, list) or not isinstance(tot, dict):
        return errs
    declared = tot.get("final_amount_total")
    if declared is None:
        return errs
    try:
        target = Decimal(str(declared))
    except Exception:
        return ["totals.final_amount_total not numeric"]

    s = Decimal(0)
    for r in rows:
        if not isinstance(r, dict) or r.get("is_omitted"):
            continue
        try:
            s += Decimal(str(r.get("final_amount") or 0))
        except Exception:
            errs.append("non_numeric final_amount in a row")
            return errs
    if abs(s - target) > tol:
        errs.append(f"totals_mismatch sum_rows={s} totals.final_amount_total={target}")
    return errs


def summary_rows_include_role_code(obj: dict[str, Any], role_code: str) -> bool:
    rc = (role_code or "").strip().upper()
    rows = obj.get("summary_rows")
    if not isinstance(rows, list):
        return False
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("role_code") or "").strip().upper() == rc:
            return True
        if str(r.get("service") or "").strip().upper() == rc:
            return True
    return False
