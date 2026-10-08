from __future__ import annotations

from datetime import date
from typing import Any


def dates_inclusive(d0: date, d1: date) -> list[str]:
    if d1 < d0:
        return [d0.isoformat()]
    out: list[str] = []
    cur = d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur = cur.fromordinal(cur.toordinal() + 1)
    return out


def build_detailed_json_from_records(
    site_key: str,
    records: list[dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    dates = dates_inclusive(period_start, period_end)
    employees: list[dict[str, Any]] = []
    grid: dict[str, dict[str, str | None]] = {}
    seen: dict[str, int] = {}
    for rec in records or []:
        name0 = (
            str(rec.get("employee_name") or "").strip()
            or str(rec.get("employee_external_id") or "").strip()
            or "employee"
        )
        n = seen.get(name0, 0) + 1
        seen[name0] = n
        name = f"{name0} ({n})" if n > 1 else name0
        eid = rec.get("employee_external_id")
        eid_str = str(eid).strip() if eid not in (None, "") else None
        emp_obj: dict[str, Any] = {
            "name": name,
            "role": rec.get("role_code") or "",
            "roll_source": rec.get("roll_type") or "",
            "employee_external_id": eid_str,
        }
        hint = rec.get("clinical_role_hint")
        if isinstance(hint, str) and hint.strip():
            emp_obj["clinical_role_hint"] = hint.strip()
        employees.append(emp_obj)
        by_date = rec.get("ohc_attendance_daywise") or {}
        if not isinstance(by_date, dict):
            by_date = {}
        grid[name] = {
            d: (by_date.get(d) if by_date.get(d) not in ("", None) else None)
            for d in dates
        }
    return {
        "ok": True,
        "site": site_key,
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "employees": employees,
        "dates": dates,
        "grid": grid,
    }

