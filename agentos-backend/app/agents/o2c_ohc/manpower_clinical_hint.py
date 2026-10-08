"""
Manpower tab enrichment for MIS: designation/specialist hints, DOJ/LWD backfill, optional joiner rows.

Index keys are **employee id only** (same as original design). Rows are always scoped by matching
``OHC Name`` to the attendance ``client_site_key`` so DOJ/LWD/designation stay site-correct for the
~5% of employee ids that appear on multiple OHCs (often with different dates).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.config.settings import settings
from app.email_automation.engine.excel_reader import read_sheet

log = logging.getLogger(__name__)

_MAX_HINT_LEN = 200

_EMP_ID_BASES = frozenset(
    {"employee id", "emp id", "employeeid", "emp. id", "emp id."}
)
_SPECIALIST_BASES = frozenset({"specialist", "speciality", "specialty", "specialization"})
_DESIGNATION_BASES = frozenset(
    {
        "designation",
        "designation (as per po/kam)",
        "designation as per po/kam",
    }
)
_OHC_BASES = frozenset({"ohc name", "ohc", "site", "client-site", "client"})
_DOJ_BASES = frozenset(
    {"date of joining", "doj", "joining date", "date of join", "join date"}
)
_LWD_BASES = frozenset({"last working day", "lwd", "last working", "relieving date", "exit date"})
_MP_ROLE_BASES = frozenset({"role"})


@dataclass(frozen=True, slots=True)
class ManpowerSiteRow:
    employee_id: str
    ohc_name: str
    designation: str | None
    specialist: str | None
    mp_role: str | None
    doj_iso: str | None
    lwd_iso: str | None
    employee_name: str | None


def _header_base(canonical_key: str) -> str:
    return re.sub(r"#\d+\Z", "", canonical_key)


def _norm_emp_id(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).replace("\u00a0", " ").strip()
    return re.sub(r"\s+", " ", s)


def _norm_site_key(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip()).casefold()


def _pick_canonical_key(canonical_keys: tuple[str, ...], bases: frozenset[str]) -> str | None:
    for ck in canonical_keys:
        if _header_base(ck) in bases:
            return ck
    return None


def _clean_text(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).replace("\u00a0", " ").strip()
    if not s:
        return None
    up = s.upper()
    if up in ("#N/A", "N/A", "#REF!", "#VALUE!"):
        return None
    if len(s) > _MAX_HINT_LEN:
        s = s[:_MAX_HINT_LEN].rstrip()
    return s


def _date_to_iso(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d.isoformat()
        except ValueError:
            continue
    return None


def _read_manpower_sheet(xlsx_path: Path):
    sheet = (getattr(settings, "o2c_manpower_sheet_name", None) or "Manpower").strip() or "Manpower"
    return read_sheet(
        xlsx_path,
        sheet,
        header_hints=(
            "employee id",
            "specialist",
            "designation",
            "name",
            "role",
            "client",
            "ohc name",
            "client-site",
            "date of joining",
            "last working day",
        ),
    )


def _load_manpower_rows(xlsx_path: Path) -> list[ManpowerSiteRow]:
    if not getattr(settings, "o2c_manpower_clinical_hint_enabled", True):
        return []
    p = xlsx_path.expanduser().resolve()
    if not p.is_file():
        return []
    try:
        data = _read_manpower_sheet(p)
    except Exception as e:
        log.warning("Manpower read_sheet failed path=%s: %s", p, e)
        return []

    emp_ck = _pick_canonical_key(data.canonical_keys, _EMP_ID_BASES)
    ohc_ck = _pick_canonical_key(data.canonical_keys, _OHC_BASES)
    if not emp_ck or not ohc_ck:
        log.warning(
            "Manpower missing employee id / ohc name (sheet=%r keys=%s)",
            data.sheet_name,
            list(data.canonical_keys)[:15],
        )
        return []

    spec_ck = _pick_canonical_key(data.canonical_keys, _SPECIALIST_BASES)
    des_ck = _pick_canonical_key(data.canonical_keys, _DESIGNATION_BASES)
    doj_ck = _pick_canonical_key(data.canonical_keys, _DOJ_BASES)
    lwd_ck = _pick_canonical_key(data.canonical_keys, _LWD_BASES)
    role_ck = _pick_canonical_key(data.canonical_keys, _MP_ROLE_BASES)
    name_ck = next((ck for ck in data.canonical_keys if _header_base(ck) == "name"), None)

    out: list[ManpowerSiteRow] = []
    for row in data.rows:
        eid = _norm_emp_id(row.get(emp_ck))
        ohc = str(row.get(ohc_ck) or "").strip()
        if not eid or not ohc:
            continue
        out.append(
            ManpowerSiteRow(
                employee_id=eid,
                ohc_name=ohc,
                designation=_clean_text(row.get(des_ck)) if des_ck else None,
                specialist=_clean_text(row.get(spec_ck)) if spec_ck else None,
                mp_role=_clean_text(row.get(role_ck)) if role_ck else None,
                doj_iso=_date_to_iso(row.get(doj_ck)) if doj_ck else None,
                lwd_iso=_date_to_iso(row.get(lwd_ck)) if lwd_ck else None,
                employee_name=_clean_text(row.get(name_ck)) if name_ck else None,
            )
        )
    return out


def _manpower_row_precedence(mp: ManpowerSiteRow) -> tuple[int, int, int]:
    """Pick the richer row when the same employee id appears twice on one OHC."""
    has_dates = 1 if (mp.doj_iso or mp.lwd_iso) else 0
    has_hint = 1 if (mp.specialist or mp.designation) else 0
    name_len = len((mp.employee_name or "").strip())
    return (has_dates, has_hint, name_len)


def _index_manpower_rows_for_site(rows: list[ManpowerSiteRow]) -> dict[str, ManpowerSiteRow]:
    out: dict[str, ManpowerSiteRow] = {}
    for mp in rows:
        prev = out.get(mp.employee_id)
        if prev is None or _manpower_row_precedence(mp) > _manpower_row_precedence(prev):
            out[mp.employee_id] = mp
    return out


def _manpower_rows_by_site(rows: list[ManpowerSiteRow]) -> dict[str, list[ManpowerSiteRow]]:
    grouped: dict[str, list[ManpowerSiteRow]] = defaultdict(list)
    for mp in rows:
        grouped[_norm_site_key(mp.ohc_name)].append(mp)
    return grouped


def build_manpower_employee_index_for_site(
    xlsx_path: Path,
    client_site_key: str,
    *,
    manpower_rows: list[ManpowerSiteRow] | None = None,
) -> dict[str, ManpowerSiteRow]:
    """
    ``employee_id`` → Manpower row for rows whose site column (OHC Name / Client / …) matches ``client_site_key``.

    When the same employee id appears twice on one site, prefer the row with DOJ/LWD and designation.
    """
    sk = _norm_site_key(client_site_key)
    if manpower_rows is None:
        manpower_rows = _load_manpower_rows(xlsx_path)
    site_rows = [mp for mp in manpower_rows if _norm_site_key(mp.ohc_name) == sk]
    return _index_manpower_rows_for_site(site_rows)


def build_clinical_role_hint(
    *,
    roll_role: str | None,
    mp: ManpowerSiteRow | None,
) -> str | None:
    """
    Single LLM-facing staffing hint: attendance roll ``role_code`` + Manpower specialist/designation.

    Does not replace ``role_code`` on the record (server + LLM still use that field as before).
    """
    rr = (roll_role or "").strip()
    mp_parts: list[str] = []
    if mp:
        if mp.specialist:
            mp_parts.append(f"manpower specialist: {mp.specialist}")
        elif getattr(settings, "o2c_manpower_use_designation_for_clinical_hint", True) and mp.designation:
            mp_parts.append(f"manpower designation: {mp.designation}")
    if not mp_parts:
        return None
    parts: list[str] = []
    if rr:
        parts.append(f"attendance role: {rr}")
    parts.extend(mp_parts)
    return "; ".join(parts)


def build_manpower_clinical_hint_index(
    xlsx_path: Path,
    *,
    client_site_key: str | None = None,
) -> dict[str, str]:
    """
    Employee ID → ``clinical_role_hint``.

    Pass ``client_site_key`` for site-scoped hints (recommended). Without it, last row wins
    globally — only suitable for legacy/tests; ~5% of ids appear on multiple OHCs.
    """
    if client_site_key:
        site_index = build_manpower_employee_index_for_site(xlsx_path, client_site_key)
        out: dict[str, str] = {}
        for eid, mp in site_index.items():
            hint = build_clinical_role_hint(roll_role=mp.mp_role, mp=mp)
            if hint:
                out[eid] = hint
        return out

    out = {}
    for mp in _load_manpower_rows(xlsx_path):
        hint = build_clinical_role_hint(roll_role=mp.mp_role, mp=mp)
        if hint:
            out[mp.employee_id] = hint
    return out


def _manpower_churn_in_billing_period(
    mp: ManpowerSiteRow,
    *,
    period_start: date,
    period_end: date,
) -> bool:
    from app.agents.o2c_ohc.mis_handover_attendance import parse_sheet_date

    lwd = parse_sheet_date(mp.lwd_iso, period_start=period_start)
    doj = parse_sheet_date(mp.doj_iso, period_start=period_start)
    return (lwd is not None and period_start <= lwd <= period_end) or (
        doj is not None and period_start <= doj <= period_end
    )


def _append_manpower_stub_row(
    rows: list[dict[str, Any]],
    *,
    site_key: str,
    mp: ManpowerSiteRow,
) -> None:
    eid = mp.employee_id
    mp_role = mp.mp_role or "Doctor"
    rows.append(
        {
            "client_site_key": site_key,
            "employee_external_id": eid[:120],
            "employee_name": mp.employee_name,
            "role_code": mp_role,
            "clinical_role_hint": build_clinical_role_hint(roll_role=mp_role, mp=mp),
            "doj": mp.doj_iso,
            "lwd": mp.lwd_iso,
            "ohc_attendance_daywise": {},
            "present_days": 0,
            "absent_days": 0,
            "total_days": 0,
            "roll_type": "Manpower",
            "_from_manpower_only": True,
        }
    )


def append_manpower_period_churn_rows(
    records: list[dict[str, Any]],
    xlsx_path: Path,
    client_site_key: str,
    *,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    """
    Add Manpower rows missing from the attendance roll when DOJ or LWD falls in the billing period.

    Called from MIS draft (period known). Workbook cache enrich only backfills hints/dates on existing roll rows.
    """
    if not getattr(settings, "o2c_manpower_clinical_hint_enabled", True):
        return records
    if not bool(getattr(settings, "o2c_manpower_add_missing_roll_rows", True)):
        return records
    if not xlsx_path.is_file():
        return records

    out = list(records)
    site_index = build_manpower_employee_index_for_site(xlsx_path, client_site_key)
    on_roll = {
        _norm_emp_id(r.get("employee_external_id"))
        for r in out
        if isinstance(r, dict) and _norm_emp_id(r.get("employee_external_id"))
    }
    for eid, mp in site_index.items():
        if eid in on_roll:
            continue
        if not _manpower_churn_in_billing_period(mp, period_start=period_start, period_end=period_end):
            continue
        _append_manpower_stub_row(out, site_key=client_site_key, mp=mp)
        on_roll.add(eid)
    return out


def enrich_amap_with_manpower_hints(
    amap: dict[str, list[dict[str, Any]]],
    xlsx_path: Path | None,
) -> None:
    """
    Mutate ``amap`` rows in place:

    - ``clinical_role_hint`` — attendance ``role_code`` + Manpower specialist/designation (LLM only)
    - ``doj`` / ``lwd`` — backfilled from Manpower when roll cells are empty (handover; not new LLM fields)

    Period churn rows (joiners on Manpower only) are appended in MIS draft via
    :func:`append_manpower_period_churn_rows` once ``period_start`` / ``period_end`` are known.
    """
    if not getattr(settings, "o2c_manpower_clinical_hint_enabled", True) or not xlsx_path:
        xlsx_path = None

    backfill_dates = bool(getattr(settings, "o2c_manpower_backfill_doj_lwd", True))

    manpower_rows: list[ManpowerSiteRow] = []
    by_site: dict[str, list[ManpowerSiteRow]] = {}
    if xlsx_path and xlsx_path.is_file():
        manpower_rows = _load_manpower_rows(xlsx_path)
        by_site = _manpower_rows_by_site(manpower_rows)

    for site_key, rows in (amap or {}).items():
        if not isinstance(rows, list):
            continue
        site_index = _index_manpower_rows_for_site(by_site.get(_norm_site_key(site_key), []))

        on_roll: set[str] = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            eid = _norm_emp_id(r.get("employee_external_id"))
            if eid:
                on_roll.add(eid)
            mp = site_index.get(eid) if eid else None

            roll_role = str(r.get("role_code") or "").strip() or None
            r["clinical_role_hint"] = build_clinical_role_hint(roll_role=roll_role, mp=mp)

            if backfill_dates and mp:
                if not str(r.get("doj") or "").strip() and mp.doj_iso:
                    r["doj"] = mp.doj_iso
                if not str(r.get("lwd") or "").strip() and mp.lwd_iso:
                    r["lwd"] = mp.lwd_iso

