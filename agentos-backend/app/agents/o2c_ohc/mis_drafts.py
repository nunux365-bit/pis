"""
O2C_OHC MIS-first draft generation.

Generates per-site MIS drafts from:
- roll_full attendance parsing (On Roll + Off Roll tabs)
- contract_rate_line rows (agenos)

Drafts are stored in DB for human review/insert/delete.

Actuals:
- `as_per_actuals` lines: the LLM reads contract text/rules for fallback when actuals are absent;
  optional structured keys in `billing_rules` can supply a deterministic fallback after the LLM.
- Contracts with ``contract_terms_version.billing_profile`` = ``taco`` (visit-session MIS bundle): optional
  **`billing_rules.ohc_mis_force_visit_session_bundled`** and **`ohc_mis_force_map_staff_duty_cap`** can pin,
  per **``contract_rate_line_id``**, whether amounts follow **visit-count** spines vs **MAP-style** spines
  (calendar / staff-duty-cap language in the contract) before falling back to prose-only classification.
  Precedence vs free-text rules is defined in that profile’s deterministic MIS engine and ``billing_rules``.

Site mapping (``site_alias``) vs LLM auto-unlink:
- When billable ``contract_terms_version`` cannot be resolved for the run period, the pipeline may delete
  ``ohc_llm_match`` aliases for that attendance label and retry matching—**unless** an **approved** MIS already
  exists for the same ``service_site_id`` and ``client_site_key`` (any billing period), treating the mapping as proven.
  Deliberate removal or correction of a wrong mapping is **not** meant to rely on auto-unlink; use a **controlled**
  flow (e.g. dedicated unlink / reingest / addendum tooling) so attendance re-processing and contract data stay
  explicit and auditable.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal, run_agenos_async
from app.agents.o2c_ohc.attendance_alias_llm import (
    SOURCE_LLM,
    delete_ohc_llm_match_aliases_for_label_async,
    try_llm_site_match_and_insert_alias_async,
    winning_site_alias_source_async,
)
from app.agents.o2c_ohc.attendance_site_recon import (
    clear_open_o2c_attendance_site_recon_for_period_key_async,
    persist_o2c_attendance_site_recon_async,
)
from app.agents.o2c_ohc.mis_details import build_detailed_json_from_records
from app.agents.o2c_ohc.manpower_clinical_hint import append_manpower_period_churn_rows
from app.agents.o2c_ohc.mis_handover_attendance import (
    apply_handover_attendance_prep,
    apply_handover_summary_row_fixup,
)
from app.integrations.gdrive_o2c import contract_folder_segment_for_mis
from app.agents.o2c_ohc.mis_display_roles import apply_display_role_labels as _apply_display_role_labels
from app.agents.o2c_ohc.mis_db import (
    apply_absent_days_locks_to_attendance_records,
    fetch_absent_days_locks_session,
    fetch_mis_run_xlsx_bundle,
    get_past_corrections_for_site,
    reapply_absent_days_locks_session,
)
from app.services.o2c.mis_contract_lines import skip_global_crl_when_site_override_exists_sql
from app.agents.o2c_ohc.billing_profile import BILLING_PROFILE_GENERIC, normalize_billing_profile
from app.agents.o2c_ohc.billing_constants import (
    INVOICE_ADMIN_PCT_BILLING_RULE_KEY,
    OHC_INVOICE_ADMIN_ROLE_CODE,
    SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE,
)
from app.agents.o2c_ohc.mis_summary_llm import (
    MIS_SERVICE_CHARGE_EXTERNAL_ID,
    NON_EMPLOYEE_SENTINEL,
    llm_generate_mis_summary_json_async,
)
from app.agents.o2c_ohc.o2c_utils import O2cAttendanceSiteSkip, _working_days_inclusive
from app.config.settings import settings
from app.infra.sync_bridge import run_blocking

log = logging.getLogger(__name__)


def _decimal(v: Any) -> Decimal:
    if v is None or v == "":
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


def _attendance_paid_days_for_headcount_rank(ar: dict[str, Any]) -> Decimal:
    """Align server cap with MIS prompts / slim attendance: prefer `paid_days`, else present+leave."""
    pd_raw = ar.get("paid_days")
    if pd_raw is not None and pd_raw != "":
        try:
            d = _decimal(pd_raw)
            if d >= 0:
                return d
        except Exception:
            pass
    return _decimal(ar.get("present_days")) + _decimal(ar.get("leave_days"))


def _safe_file_part(v: Any) -> str:
    """
    Normalize dynamic filename parts so site keys like "Unit I/Phase I"
    do not create unintended sub-directories.
    """
    s = str(v or "").strip()
    if not s:
        return "na"
    s = re.sub(r"[\\/:*?\"<>|]+", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200] or "na"


def _days_inclusive(start: date, end: date) -> int:
    if end < start:
        return 1
    return (end - start).days + 1


def _bucket_attendance_status(v: Any) -> str:
    if v is None:
        return "blank"
    s = str(v).strip()
    if not s:
        return "blank"
    sl = s.lower()
    sl_flat = re.sub(r"[\s_-]+", "", sl)
    if "halfday" in sl_flat and "present" not in sl:
        return "half_day"
    if "absent" in sl and "present" not in sl:
        return "absent"
    if "week off" in sl or sl.replace(" ", "") == "weekoff" or "holiday" in sl:
        return "week_off"
    if "leave" in sl and "present" not in sl:
        return "leave"
    if sl == "left":
        return "not_applicable"
    return "present"


def _attendance_records_for_period(
    records: list[dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    """
    Normalize attendance metrics to the exact billing period using daywise statuses when available.
    This prevents date-range spillover (e.g. Mar days inside Feb billing period).
    """
    out: list[dict[str, Any]] = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        daywise = rec.get("ohc_attendance_daywise")
        if not isinstance(daywise, dict) or not daywise:
            out.append(dict(rec))
            continue

        counts = {
            "present_days": Decimal("0"),
            "absent_days": Decimal("0"),
            "week_off_days": Decimal("0"),
            "leave_days": Decimal("0"),
            "blank_days": Decimal("0"),
        }
        scoped_daywise: dict[str, Any] = {}
        for ds, st in daywise.items():
            try:
                d = date.fromisoformat(str(ds))
            except Exception:
                continue
            if d < period_start or d > period_end:
                continue
            scoped_daywise[ds] = st
            b = _bucket_attendance_status(st)
            if b == "absent":
                counts["absent_days"] += Decimal("1")
            elif b == "half_day":
                counts["present_days"] += Decimal("0.5")
                counts["absent_days"] += Decimal("0.5")
            elif b == "week_off":
                counts["week_off_days"] += Decimal("1")
            elif b == "leave":
                counts["leave_days"] += Decimal("1")
            elif b == "blank":
                counts["blank_days"] += Decimal("1")
            elif b == "not_applicable":
                pass
            else:
                counts["present_days"] += Decimal("1")

        total_days = len(scoped_daywise)
        expected_days = max(Decimal("0"), Decimal(total_days) - counts["week_off_days"])
        upd = dict(rec)
        upd.update(
            {
                "present_days": counts["present_days"],
                "absent_days": counts["absent_days"],
                "week_off_days": float(counts["week_off_days"]),
                "leave_days": float(counts["leave_days"]),
                "blank_days": float(counts["blank_days"]),
                "total_days": total_days,
                "expected_days": expected_days,
                "ohc_attendance_daywise": scoped_daywise,
                "ohc_attendance_days": total_days,
            }
        )
        out.append(upd)
    return out


def _apply_fmo_mo_attendance_hints(
    attendance_records: list[dict[str, Any]],
    rate_lines: list[dict[str, Any]],
) -> None:
    """
    When the contract has **both** full medical officer lines (contract ``role_code`` prefix ``FMO_*``)
    and staff medical officer lines (``MO_*``, ``SR_MO_*``, ``JR_MO_*``, ``COMPANY_MO_*``) as
    ``rate_attendance``, but **every** physician on the attendance roster is labeled only with MO-family
    roles, the MIS model may leave FMO seats empty. Tag up to FMO headcount with
    ``mis_staffing_slot_hint`` / ``mis_staffing_fmo_role_code``.

    **No-op** when attendance already includes ``FMO_*`` labels, when there is only one physician row, or
    when the contract has no FMO rate line — use ``_flag_fmo_contract_line_missing_vs_attendance`` when
    attendance shows FMO but the contract extract omitted the FMO line.
    """
    staff = [
        rl
        for rl in (rate_lines or [])
        if str(rl.get("billing_model") or "").strip().lower() == "rate_attendance"
    ]
    fmo_lines = [rl for rl in staff if str(rl.get("role_code") or "").strip().upper().startswith("FMO_")]
    mo_lines = [
        rl
        for rl in staff
        if not str(rl.get("role_code") or "").strip().upper().startswith("FMO_")
        and (
            str(rl.get("role_code") or "").strip().upper().startswith("MO_")
            or str(rl.get("role_code") or "").strip().upper().startswith("SR_MO_")
            or str(rl.get("role_code") or "").strip().upper().startswith("JR_MO_")
            or str(rl.get("role_code") or "").strip().upper().startswith("COMPANY_MO_")
        )
    ]
    if not fmo_lines or not mo_lines:
        return

    contract_rc = {
        str(rl.get("role_code") or "").strip().upper()
        for rl in fmo_lines + mo_lines
        if str(rl.get("role_code") or "").strip()
    }
    fmo_rc_set = {str(rl.get("role_code") or "").strip().upper() for rl in fmo_lines if rl.get("role_code")}
    fmo_primary = str(sorted(fmo_lines, key=lambda x: str(x.get("role_code") or ""))[0].get("role_code") or "").strip()

    def _physician_attendance_row(rec: dict[str, Any]) -> bool:
        rc = str(rec.get("role_code") or "").strip().upper()
        if not rc:
            return False
        if rc in contract_rc:
            return True
        return rc.startswith(("MO_", "FMO_", "SR_MO_", "JR_MO_", "COMPANY_MO_"))

    pool = [r for r in (attendance_records or []) if isinstance(r, dict) and _physician_attendance_row(r)]
    if len(pool) < 2:
        return

    def _paid_days(rec: dict[str, Any]) -> Decimal:
        return _decimal(rec.get("present_days")) + _decimal(rec.get("leave_days"))

    def _sort_key(rec: dict[str, Any]) -> tuple[float, str]:
        return (-float(_paid_days(rec)), str(rec.get("employee_external_id") or ""))

    def _already_fmo_labeled(rec: dict[str, Any]) -> bool:
        rc = str(rec.get("role_code") or "").strip().upper()
        if rc in fmo_rc_set:
            return True
        return bool(rc.startswith("FMO_") and fmo_rc_set)

    def _fmo_role_for_rec(rec: dict[str, Any]) -> str | None:
        rc = str(rec.get("role_code") or "").strip().upper()
        if rc in fmo_rc_set:
            return rc
        return fmo_primary or None

    prefer_fmo = sorted([r for r in pool if _already_fmo_labeled(r)], key=_sort_key)
    mo_side_only = sorted([r for r in pool if not _already_fmo_labeled(r)], key=_sort_key)
    if not mo_side_only:
        return

    fmo_seats = 0
    for rl in fmo_lines:
        try:
            q = int(_decimal(rl.get("contracted_quantity") or 1))
        except Exception:
            q = 1
        fmo_seats += max(1, q)
    fmo_seats = max(1, fmo_seats)

    k = min(fmo_seats, len(pool))
    chosen: list[dict[str, Any]] = []
    for bucket in (prefer_fmo, mo_side_only):
        for r in bucket:
            if len(chosen) >= k:
                break
            chosen.append(r)
        if len(chosen) >= k:
            break

    for r in chosen:
        hint_rc = _fmo_role_for_rec(r)
        if not hint_rc:
            continue
        r["mis_staffing_slot_hint"] = "fmo_preferred"
        r["mis_staffing_fmo_role_code"] = hint_rc


# Attendance labels FMO_* but contract ``rate_lines`` have no FMO staffing row (e.g. extract only MO_*).
MIS_STAFFING_CONTRACT_GAP_FMO_LINE_MISSING = "fmo_labeled_attendance_no_fmo_contract_line"


def _flag_fmo_contract_line_missing_vs_attendance(
    attendance_records: list[dict[str, Any]],
    rate_lines: list[dict[str, Any]],
) -> None:
    """
    When attendance records use full medical officer roles (``FMO_*``) but the contract has no
    ``FMO_*`` ``rate_attendance`` line (only MO-family lines), the MIS model must not silently map those
    people onto a staff MO line. Tag affected attendance rows for the prompt; the durable fix is
    **contract re-extract** so FMO and MO lines match the deal.
    """
    staff = [
        rl
        for rl in (rate_lines or [])
        if str(rl.get("billing_model") or "").strip().lower() == "rate_attendance"
    ]
    if any(str(rl.get("role_code") or "").strip().upper().startswith("FMO_") for rl in staff):
        return
    for r in attendance_records or []:
        if not isinstance(r, dict):
            continue
        rc = str(r.get("role_code") or "").strip().upper()
        if rc.startswith("FMO_"):
            r["mis_staffing_contract_gap"] = MIS_STAFFING_CONTRACT_GAP_FMO_LINE_MISSING


def _repair_summary_row_employee_external_ids(
    summary_json: dict[str, Any],
    attendance_records: list[dict[str, Any]],
) -> None:
    """
    Rows are persisted only when ``employee_external_id`` matches an attendance id.

    Fills empty id from unique ``employee_name``; fixes wrong id via unique name match; if still
    wrong, maps ``NA-158``-style LLM codes to purely numeric sheet ids (e.g. ``158``) when exactly
    one attendance row matches.

    **Site scope:** single-site ``attendance_records`` only (same slice as the LLM).

    For production flow, call via ``_mis_post_llm_repair_employee_external_ids`` so the post-LLM
    sequence stays obvious; tests may call this function directly.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    valid_ids = {
        str(r.get("employee_external_id") or "").strip()
        for r in (attendance_records or [])
        if str(r.get("employee_external_id") or "").strip()
    }
    by_name_lower: dict[str, set[str]] = defaultdict(set)
    for r in attendance_records or []:
        eid = str(r.get("employee_external_id") or "").strip()
        nm = str(r.get("employee_name") or "").strip().lower()
        if eid and nm:
            by_name_lower[nm].add(eid)

    for rr in rows:
        if not isinstance(rr, dict):
            continue
        eid = str(rr.get("employee_external_id") or "").strip()
        ename = str(rr.get("employee_name") or "").strip()

        if eid == MIS_SERVICE_CHARGE_EXTERNAL_ID:
            continue

        # 1) Missing id: unique name match only (employee rows).
        if not eid and ename:
            c = by_name_lower.get(ename.lower(), set())
            if len(c) == 1:
                fixed = next(iter(c))
                log.info(
                    "MIS employee_external_id repair: (empty) -> %r (unique attendance name match)",
                    fixed,
                )
                rr["employee_external_id"] = fixed
            continue

        if not eid:
            continue

        if eid in valid_ids:
            continue

        # 2) Wrong id: unique name match (existing behaviour).
        candidates: set[str] = set()
        if ename:
            candidates |= by_name_lower.get(ename.lower(), set())
        if eid.lower() != ename.lower():
            candidates |= by_name_lower.get(eid.lower(), set())
        if len(candidates) == 1:
            fixed = next(iter(candidates))
            log.info(
                "MIS employee_external_id repair: %r -> %r (unique attendance name match)",
                eid,
                fixed,
            )
            rr["employee_external_id"] = fixed
            continue

        # 3) LLM ``NA-158`` vs sheet ``data_only`` numeric id ``158`` (one match only).
        el = eid.strip().lower().replace("_", "-")
        matched: list[str] = []
        seen_a: set[str] = set()
        for r in attendance_records or []:
            aid = str(r.get("employee_external_id") or "").strip()
            if not aid or aid in seen_a:
                continue
            ok = aid.lower() == el
            if not ok:
                m = re.fullmatch(r"(\d+)(?:\.0+)?", aid)
                if m:
                    d = m.group(1)
                    ok = el == d or el == f"na-{d}"
            if ok:
                matched.append(aid)
                seen_a.add(aid)
        if len(matched) == 1:
            fixed = matched[0]
            log.info("MIS employee_external_id repair: %r -> %r (id / NA- alignment)", eid, fixed)
            rr["employee_external_id"] = fixed


def _staffing_subtotal_when_no_per_line_service_charge(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> Decimal | None:
    """
    Sum of non-omitted MIS ``final_amount`` for ``rate_attendance`` contract lines.

    Returns None when any ``rate_attendance`` line still has a non-``none`` ``service_charge_type``.
    Those lines usually embed per-line admin or uplift in the row amount (common when
    ``billing_profile=tcs``); invoice-level admin % must not be recomputed from this subtotal.
    """
    for rl in rate_lines:
        if not isinstance(rl, dict):
            continue
        if str(rl.get("billing_model") or "").strip().lower() != "rate_attendance":
            continue
        st = str(rl.get("service_charge_type") or "").strip().lower()
        if st in ("none", "", "null", "-"):
            continue
        return None
    staffing_ids = {
        str(rl["id"])
        for rl in rate_lines
        if rl.get("id") and str(rl.get("billing_model") or "").strip().lower() == "rate_attendance"
    }
    if not staffing_ids:
        return None
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return None
    total = Decimal(0)
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        if bool(rr.get("is_omitted")):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        if crl not in staffing_ids:
            continue
        emp = str(rr.get("employee_external_id") or "").strip()
        if emp == MIS_SERVICE_CHARGE_EXTERNAL_ID:
            continue
        try:
            total += Decimal(str(rr.get("final_amount") or 0))
        except Exception:
            pass
    return total


def _has_server_tac_o_invoice_admin_line(rate_lines: list[dict[str, Any]]) -> bool:
    """True when ingest added the invoice-admin rate line with the taco-profile server marker in ``source_ref``.

    Distinguishes that row from admin lines created manually or for other ``billing_profile`` values.
    """
    for rl in rate_lines or []:
        if not isinstance(rl, dict):
            continue
        if str(rl.get("role_code") or "").strip().upper() != OHC_INVOICE_ADMIN_ROLE_CODE:
            continue
        sr = rl.get("source_ref")
        if isinstance(sr, dict) and str(sr.get("note") or "").strip() == SERVER_TACO_INVOICE_ADMIN_SOURCE_NOTE:
            return True
    return False


def _reconcile_invoice_admin_pct_rows_from_staffing(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> None:
    """
    When ``_has_server_tac_o_invoice_admin_line`` is true and every staffing ``rate_attendance`` line
    uses no per-line ``service_charge_type``, set invoice-admin ``final_amount`` = configured % × staffing
    subtotal (same basis as ingest tagged that row).

    No-op when staffing still has per-line service charges (typical ``billing_profile=tcs``), or when the
    admin line lacks the ingest marker.
    """
    if not _has_server_tac_o_invoice_admin_line(rate_lines):
        return
    subtotal = _staffing_subtotal_when_no_per_line_service_charge(summary_json, rate_lines)
    if subtotal is None:
        return
    admin_lines = [
        rl
        for rl in rate_lines
        if isinstance(rl, dict)
        and str(rl.get("role_code") or "").strip().upper() == OHC_INVOICE_ADMIN_ROLE_CODE
    ]
    if not admin_lines:
        return
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    for arl in admin_lines:
        aid = str(arl.get("id") or "").strip()
        if not aid:
            continue
        br = arl.get("billing_rules")
        if not isinstance(br, dict):
            continue
        raw_pct = br.get(INVOICE_ADMIN_PCT_BILLING_RULE_KEY)
        try:
            pct = Decimal(str(raw_pct))
        except Exception:
            continue
        if pct <= 0:
            continue
        admin_amt = (subtotal * pct / Decimal(100)).quantize(Decimal("0.01"))
        tag = (
            f"[server] Invoice admin = {pct}% × staffing subtotal {subtotal} = {admin_amt} "
            "(aligned with taco-profile ingest when staffing has no per-line service_charge)."
        )
        for rr in rows:
            if not isinstance(rr, dict):
                continue
            if str(rr.get("contract_rate_line_id") or "").strip() != aid:
                continue
            rr["final_amount"] = float(admin_amt)
            note = str(rr.get("calc_notes") or "").strip()
            rr["calc_notes"] = f"{tag} {note}".strip() if note else tag


def _quantize_mis_summary_final_amounts_currency(summary_json: dict[str, Any]) -> None:
    """
    Generic MIS: LLM emits full-precision ``final_amount``; persist INR half-up to 0.01.
    Omitted rows stay 0. ``calc_notes`` unchanged.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        fa = rr.get("final_amount")
        if fa is None:
            continue
        try:
            d = Decimal(str(fa)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            rr["final_amount"] = float(d)
        except Exception:
            continue


_CALC_NOTES_FINAL_AMOUNT_RE = re.compile(
    r"final_amount\s*[=:]\s*"
    r"([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\d*\.\d+)(?:[eE][+-]?\d+)?",
    re.IGNORECASE,
)


def _parse_terminal_final_amount_from_calc_notes(text: str) -> Decimal | None:
    """Last ``final_amount=<number>`` / ``final_amount: <number>`` in calc_notes (commas allowed)."""
    if not text or not isinstance(text, str):
        return None
    matches = list(_CALC_NOTES_FINAL_AMOUNT_RE.finditer(text))
    if not matches:
        return None
    raw = matches[-1].group(1).replace(",", "")
    try:
        return Decimal(raw)
    except Exception:
        return None


def _apply_calc_notes_amount_to_summary_rows(summary_json: dict[str, Any]) -> None:
    """
    Ensure each row has ``calc_notes_amount``.

    Prefer the LLM's ``calc_notes_amount`` when present and numeric; otherwise the terminal
    ``final_amount=`` / ``final_amount:`` in ``calc_notes``; else ``final_amount``.

    When ``calc_notes_amount`` (after resolution) differs from ``final_amount`` on cents
    boundaries, set ``final_amount`` to match ``calc_notes_amount`` so line items and totals
    stay consistent with the calculation trail.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    q = Decimal("0.01")

    def _align_final_amount_if_mismatch(rr: dict[str, Any]) -> None:
        cna = rr.get("calc_notes_amount")
        if cna is None:
            return
        try:
            d_cna = _decimal(cna).quantize(q)
            d_fa = _decimal(rr.get("final_amount")).quantize(q)
            if d_cna != d_fa:
                rr["final_amount"] = float(d_cna)
        except Exception:
            pass

    for rr in rows:
        if not isinstance(rr, dict):
            continue
        raw_cna = rr.get("calc_notes_amount")
        if raw_cna is not None and raw_cna != "":
            try:
                rr["calc_notes_amount"] = float(_decimal(raw_cna))
                _align_final_amount_if_mismatch(rr)
                continue
            except Exception:
                pass
        notes = str(rr.get("calc_notes") or "")
        parsed = _parse_terminal_final_amount_from_calc_notes(notes)
        if parsed is not None:
            rr["calc_notes_amount"] = float(parsed)
        else:
            try:
                rr["calc_notes_amount"] = float(_decimal(rr.get("final_amount")))
            except Exception:
                rr["calc_notes_amount"] = 0.0
        _align_final_amount_if_mismatch(rr)


def _sync_mis_summary_totals(data: dict[str, Any]) -> None:
    """
    Force totals.final_amount_total = sum(final_amount) for non-omitted summary_rows,
    and align rows_included / rows_omitted with the list. LLM output can drift; DB audit
    and UI should match arithmetic on line items.
    """
    rows = data.get("summary_rows")
    if not isinstance(rows, list):
        return
    included = 0
    omitted = 0
    total = Decimal(0)
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        if bool(rr.get("is_omitted")):
            omitted += 1
        else:
            included += 1
            total += _decimal(rr.get("final_amount"))
    blob = data.setdefault("totals", {})
    if not isinstance(blob, dict):
        blob = {}
        data["totals"] = blob
    blob["final_amount_total"] = float(total.quantize(Decimal("0.01")))
    blob["rows_included"] = included
    blob["rows_omitted"] = omitted


def _site_key_slug(value: Any) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _apply_shared_resource_primary_only_adjustments(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
    *,
    client_site_key: str,
    service_site_id: str,
) -> None:
    """
    Deterministic guardrail: if resolver marked a line as shared_resource.primary_only,
    bill only on the resolved primary site. For non-primary rows keep line-item visible
    but set final_amount=0 with explicit reason.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    current_slug = _site_key_slug(client_site_key)
    current_site_id = str(service_site_id or "").strip()
    if not current_slug and not current_site_id:
        return
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        if str(rr.get("employee_external_id") or "").strip() == MIS_SERVICE_CHARGE_EXTERNAL_ID:
            continue
        crl_id = str(rr.get("contract_rate_line_id") or "").strip()
        rl = rl_by_id.get(crl_id)
        if not rl:
            continue
        if str(rl.get("role_code") or "").strip().upper() == OHC_INVOICE_ADMIN_ROLE_CODE:
            continue
        br = rl.get("billing_rules")
        if not isinstance(br, dict):
            continue
        shared = br.get("shared_resource")
        if not isinstance(shared, dict):
            continue
        if str(shared.get("status") or "").strip().lower() != "resolved":
            continue
        if str(shared.get("mode") or "").strip().lower() != "primary_only":
            continue
        primary_site_id = str(shared.get("primary_service_site_id") or "").strip()
        primary_slug = _site_key_slug(shared.get("primary_site_slug") or shared.get("primary_site_key"))
        is_primary = False
        if primary_site_id and current_site_id:
            is_primary = primary_site_id == current_site_id
        elif primary_slug and current_slug:
            is_primary = primary_slug == current_slug
        if is_primary:
            continue
        group_id = str(shared.get("group_id") or "").strip() or "shared_resource_group"
        rr["final_amount"] = 0.0
        rr["omit_reason"] = f"shared_resource_non_primary:{group_id}"
        note = str(rr.get("calc_notes") or "").strip()
        extra = f"[server] Shared resource primary_only; billed on primary site '{primary_slug}'."
        rr["calc_notes"] = f"{note} {extra}".strip() if note else extra


def _apply_contract_defaults_to_summary_rows(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> None:
    """
    Align contracted_rate / contracted_count with contract_rate_line so Excel shows the
    contractual cap/fixed rate (e.g. BMW) even when final_amount is 0 pending actuals.

    **service** = short label from ``role_code`` (e.g. Doctor, ACLS_AMBULANCE, BMW_DISPOSAL),
    not the long contract description. Full description is appended to **comments** when missing.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        if str(rr.get("employee_external_id") or "").strip() == MIS_SERVICE_CHARGE_EXTERNAL_ID:
            continue
        crl_id = str(rr.get("contract_rate_line_id") or "").strip()
        rl = rl_by_id.get(crl_id)
        if not rl:
            continue
        cq = rl.get("contracted_quantity")
        if cq is not None:
            try:
                rr["contracted_count"] = float(_decimal(cq))
            except Exception:
                pass
        bm = str(rl.get("billing_model") or "").lower().strip()
        ra = _decimal(rl.get("rate_amount"))
        if ra > 0:
            rr["contracted_rate"] = float(ra)
        elif bm == "as_per_actuals":
            # Display a meaningful contract reference rate for as_per_actuals lines
            # when rate_amount is null/0 but contract defines cap/fallback in rules/text.
            fb = _as_per_actuals_fallback_inr_from_rate_line(rl)
            if fb is None or fb <= 0:
                fb = _extract_inr_amount_from_contract_text(rl.get("billing_rule_text"))
            if fb is not None and fb > 0:
                rr["contracted_rate"] = float(fb)

        desc = (rl.get("description") or "").strip()
        rc = (rl.get("role_code") or "").strip()
        if rc:
            rr["service"] = rc[:500]
        elif desc:
            rr["service"] = desc[:120]
        if desc:
            existing = str(rr.get("comments") or "").strip()
            if desc not in existing:
                rr["comments"] = (
                    f"{existing} | {desc}".strip(" |") if existing else desc
                )[:2000]


def _apply_non_employee_fixed_final_defaults(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> None:
    """
    Non-employee lines (ambulance, fixed fees): if the LLM left final_amount null, set it from
    rate_amount × contracted_quantity for models that are not purely actuals-driven.
    as_per_actuals (e.g. BMW) stays 0 until actuals exist — do not invent an amount here.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        emp = rr.get("employee_external_id")
        if emp is not None and str(emp).strip():
            continue
        crl_id = str(rr.get("contract_rate_line_id") or "").strip()
        rl = rl_by_id.get(crl_id)
        if not rl:
            continue
        bm = str(rl.get("billing_model") or "").lower().strip()
        if bm == "as_per_actuals":
            continue
        ra = _decimal(rl.get("rate_amount"))
        if ra <= 0:
            continue
        cq = _decimal(rl.get("contracted_quantity") or 1)
        if cq <= 0:
            cq = Decimal(1)
        target = float((ra * cq).quantize(Decimal("0.01")))
        if rr.get("final_amount") is None:
            rr["final_amount"] = target


def _as_per_actuals_fallback_inr_from_rate_line(rl: dict[str, Any]) -> Decimal | None:
    """
    Machine-readable contract fallback when actuals are not in the MIS payload.
    Ingestion may populate billing_rules with these keys; optional bill_rate_amount_when_no_actuals
    uses rate_amount × contracted_quantity when True.
    """
    br = rl.get("billing_rules")
    if not isinstance(br, dict):
        return None
    for key in (
        "when_actuals_missing",
        "fallback_without_actuals_inr",
        "minimum_without_actuals_inr",
        "default_charge_without_actuals_inr",
        "no_actuals_amount_inr",
    ):
        if key in br:
            d = _decimal(br[key])
            if d > 0:
                return d
    for nested_key in ("actuals", "as_per_actuals", "pass_through"):
        sub = br.get(nested_key)
        if not isinstance(sub, dict):
            continue
        for key in ("fallback_amount_inr", "minimum_monthly_inr", "charge_if_no_actuals_inr"):
            if key in sub:
                d = _decimal(sub[key])
                if d > 0:
                    return d
        if sub.get("bill_rate_amount_when_no_actuals") is True:
            ra = _decimal(rl.get("rate_amount"))
            cq = _decimal(rl.get("contracted_quantity") or 1)
            if cq <= 0:
                cq = Decimal(1)
            if ra > 0:
                return (ra * cq).quantize(Decimal("0.01"))
    return None


def _as_per_contract_signals_cap_or_fallback_when_no_actuals(rl: dict[str, Any]) -> bool:
    """
    True when the contract (structured billing_rules or stable billing_rule_text) implies we may
    bill a cap / fallback when actuals are missing — not generic as_per_actuals (equipment,
    medicines). Does not parse LLM calc_notes.
    """
    if _as_per_actuals_fallback_inr_from_rate_line(rl) is not None:
        return True
    brt = str(rl.get("billing_rule_text") or "").lower()
    if "whichever is lower" in brt:
        return True
    if "approximate monthly cost cap" in brt:
        return True
    if "lower of" in brt and ("actual" in brt or "approximate" in brt):
        return True
    return False


def _extract_inr_amount_from_contract_text(text: Any) -> Decimal | None:
    """
    Best-effort extraction of the first INR/Rs amount from billing_rule_text.
    Used only for display defaults when structured rate fields are absent.
    """
    s = str(text or "")
    if not s.strip():
        return None
    m = re.search(r"(?:\bINR\b|₹|\bRs\.?\b)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", s, flags=re.IGNORECASE)
    if not m:
        return None
    d = _decimal(m.group(1).replace(",", ""))
    return d if d > 0 else None


def _apply_as_per_actuals_fallback_from_contract(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> None:
    """
    If the LLM left final_amount null/0 for a non-employee as_per_actuals line but billing_rules
    encodes an amount when actuals are missing, apply it (does not override a positive LLM amount).
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        emp = rr.get("employee_external_id")
        if emp is not None and str(emp).strip():
            continue
        crl_id = str(rr.get("contract_rate_line_id") or "").strip()
        rl = rl_by_id.get(crl_id)
        if not rl:
            continue
        if str(rl.get("billing_model") or "").lower().strip() != "as_per_actuals":
            continue
        fa = rr.get("final_amount")
        if fa is not None and _decimal(fa) > 0:
            continue
        fb = _as_per_actuals_fallback_inr_from_rate_line(rl)
        if fb is None or fb <= 0:
            continue
        rr["final_amount"] = float(fb)
        cn = str(rr.get("calc_notes") or "").strip()
        extra = "Contract billing_rules: amount when actuals missing (server-applied)."
        rr["calc_notes"] = f"{cn} | {extra}".strip(" |") if cn else extra


def _cap_employee_summary_rows_by_contracted_quantity(
    summary_json: dict[str, Any],
    attendance_records: list[dict[str, Any]],
    rate_lines: list[dict[str, Any]],
) -> None:
    """
    If more employee summary rows exist for a rate line than contract contracted_quantity,
    keep only the best N by paid_days (explicit field when present and non-negative, else
    present_days+leave_days), then final_amount desc, then employee id.
    Others are marked omitted with final_amount 0.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    att_by_emp = {
        str(r.get("employee_external_id") or "").strip(): r
        for r in (attendance_records or [])
        if r.get("employee_external_id")
    }

    by_crl: dict[str, list[int]] = defaultdict(list)
    for i, rr in enumerate(rows):
        if not isinstance(rr, dict):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        emp = rr.get("employee_external_id")
        if not crl or emp is None or not str(emp).strip():
            continue
        emp_s = str(emp).strip()
        if emp_s == MIS_SERVICE_CHARGE_EXTERNAL_ID or emp_s == NON_EMPLOYEE_SENTINEL:
            continue
        by_crl[crl].append(i)

    for crl_id, idxs in by_crl.items():
        rl = rl_by_id.get(crl_id)
        if not rl:
            continue
        cq = rl.get("contracted_quantity")
        if cq is None:
            continue
        try:
            cap = int(_decimal(cq))
        except Exception:
            continue
        if cap < 1:
            continue
        if len(idxs) <= cap:
            continue

        def sort_key(i: int) -> tuple:
            rr = rows[i]
            emp = str(rr.get("employee_external_id") or "").strip()
            ar = att_by_emp.get(emp) or {}
            pd = _attendance_paid_days_for_headcount_rank(ar)
            fa = _decimal(rr.get("final_amount"))
            return (-pd, -fa, emp)

        idxs_sorted = sorted(idxs, key=sort_key)
        keep = set(idxs_sorted[:cap])
        for i in idxs:
            rr = rows[i]
            if i in keep:
                # Guardrail: if LLM pre-marked a kept row as omitted, restore it as billed.
                rr["is_omitted"] = False
                rr["omit_reason"] = None
                continue
            rr["is_omitted"] = True
            rr["omit_reason"] = (
                "Not billed: contracted_quantity headcount cap — "
                f"this line allows only {cap} employee row(s); "
                f"this person is outside the top {cap} by paid_days (present+leave), "
                "then final_amount, then employee id."
            )
            rr["final_amount"] = 0.0
            note = str(rr.get("calc_notes") or "").strip()
            hdr = (
                "[server] Not billed: headcount cap (contracted_quantity="
                f"{cap}). Ranking keeps the top {cap} row(s) by paid_days desc, "
                "final_amount desc, employee_external_id asc; this row did not qualify.\n"
                "---\n"
            )
            rr["calc_notes"] = (hdr + note).strip() if note else hdr.strip()


def _mis_duplicate_employee_staffing_anomaly_message(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> str | None:
    """
    Same real employee would be **billed twice**: ``employee_external_id`` is **non-omitted** on **two or more**
    different **attendance-based** ``contract_rate_line_id`` values in one period (``rate_attendance`` or
    ``per_visit`` with ``attendance_required`` ≠ false). Triggers one LLM self-correction retry for every
    ``billing_profile`` (``taco``, ``tcs``, ``generic``).
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return None
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    emp_to_crls: dict[str, set[str]] = defaultdict(set)
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        if bool(rr.get("is_omitted")):
            continue
        emp = rr.get("employee_external_id")
        if emp is None:
            continue
        emp_s = str(emp).strip()
        if not emp_s or emp_s in (NON_EMPLOYEE_SENTINEL, MIS_SERVICE_CHARGE_EXTERNAL_ID):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        if not crl:
            continue
        rl = rl_by_id.get(crl)
        if not rl or not _mis_rate_line_is_attendance_based(rl):
            continue
        emp_to_crls[emp_s].add(crl)

    dups = sorted(emp for emp, crls in emp_to_crls.items() if len(crls) >= 2)
    if not dups:
        return None
    lines_out = "\n".join(
        f"- employee_external_id={e!r}: appears as **not omitted** (active staffing bill) on **more than one** "
        "attendance-based contract_rate_line_id for this period — same person would be billed twice."
        for e in dups
    )
    return (
        "Same employee billed twice on staffing (server merge check):\n"
        f"{lines_out}\n\n"
        "**What this means:** One real `employee_external_id` must not have `is_omitted=false` on two "
        "different attendance-based lines in the same MIS period. That is duplicate staffing billing "
        "(for example the same person kept billable on two physician contract lines, such as role codes "
        "`FMO_*` full medical officer vs `MO_*` staff medical officer).\n\n"
        "**Which lines count:** `billing_model` = `rate_attendance`, or `per_visit` where "
        "`attendance_required` is not `false`.\n\n"
        "**What to do:** Re-emit the **full** MIS Summary JSON. For each affected `employee_external_id`, "
        "keep **at most one** `is_omitted=false` row across those lines (pick the correct line from contract "
        "+ attendance). Every other row for that same person on those lines must be `is_omitted=true`, "
        "`final_amount=0`, with `omit_reason` (not billed on this line / routing loser)."
    )


def _mis_rate_line_is_attendance_based(rl: dict[str, Any]) -> bool:
    """
    Roll-tied staffing lines for MIS guards (coerce / duplicate / roll-id anomaly).

    Uses the same ``contract_rate_line`` fields as the LLM payload: ``billing_model`` and
    ``attendance_required``. Both ``rate_attendance`` and ``per_visit`` count only when
    ``attendance_required`` is not ``False`` (DB has a small number of ``rate_attendance`` rows
    with ``attendance_required=false`` (for example ambulance or other services not tied to roster
    attendance); those are excluded here).
    """
    bm = str(rl.get("billing_model") or "").lower().strip()
    if bm not in ("rate_attendance", "per_visit"):
        return False
    return rl.get("attendance_required") is not False


def _mis_row_roll_employee_external_id(
    rr: dict[str, Any],
    valid_ids: set[str],
) -> str | None:
    """Non-empty id present on the period roll (not LINE_ITEM / service-charge sentinels)."""
    emp = rr.get("employee_external_id")
    emp_s = str(emp).strip() if emp is not None and str(emp).strip() else ""
    if not emp_s or emp_s in (NON_EMPLOYEE_SENTINEL, MIS_SERVICE_CHARGE_EXTERNAL_ID):
        return None
    if emp_s not in valid_ids:
        return None
    return emp_s


def _coerce_attendance_staffing_rows_without_roll_id_inplace(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
    attendance_records: list[dict[str, Any]],
) -> None:
    """
    Attendance-based rate lines: rows without a real ``employee_external_id`` on the roll are
    forced to ``is_omitted=true``, ``final_amount=0`` before downstream cap / totals.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    valid_ids = {
        str(r.get("employee_external_id") or "").strip()
        for r in (attendance_records or [])
        if isinstance(r, dict) and str(r.get("employee_external_id") or "").strip()
    }
    hdr = (
        "[server] Attendance-based rate line: no matching employee_external_id in attendance_records "
        "for this site/period — row treated as omitted (final_amount=0).\n---\n"
    )
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        if not crl:
            continue
        rl = rl_by_id.get(crl)
        if not rl or not _mis_rate_line_is_attendance_based(rl):
            continue
        if _mis_row_roll_employee_external_id(rr, valid_ids) is not None:
            continue
        emp_s = str(rr.get("employee_external_id") or "").strip()
        if (
            bool(rr.get("is_omitted"))
            and _decimal(rr.get("final_amount")) == 0
            and (
                emp_s == NON_EMPLOYEE_SENTINEL
                or emp_s == MIS_SERVICE_CHARGE_EXTERNAL_ID
            )
        ):
            continue
        rr["is_omitted"] = True
        rr["final_amount"] = 0.0
        rr["employee_external_id"] = NON_EMPLOYEE_SENTINEL
        rr["employee_name"] = None
        prev = str(rr.get("omit_reason") or "").strip()
        rr["omit_reason"] = (hdr + prev).strip() if prev else hdr.strip()
        note = str(rr.get("calc_notes") or "").strip()
        rr["calc_notes"] = (hdr + note).strip() if note else hdr.strip()


def _mis_attendance_staffing_roll_id_anomaly_message(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
    attendance_records: list[dict[str, Any]],
) -> str | None:
    """
    Same condition as ``_coerce_attendance_staffing_rows_without_roll_id_inplace``, but only flags
    **non-omitted** rows (LLM should not emit those). Used for one self-correction retry.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return None
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    valid_ids = {
        str(r.get("employee_external_id") or "").strip()
        for r in (attendance_records or [])
        if isinstance(r, dict) and str(r.get("employee_external_id") or "").strip()
    }
    issues: list[str] = []
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        if bool(rr.get("is_omitted")):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        if not crl:
            continue
        rl = rl_by_id.get(crl)
        if not rl or not _mis_rate_line_is_attendance_based(rl):
            continue
        if _mis_row_roll_employee_external_id(rr, valid_ids) is not None:
            continue
        emp = rr.get("employee_external_id")
        emp_s = str(emp).strip() if emp is not None and str(emp).strip() else ""
        issues.append(
            f"contract_rate_line_id={crl}: attendance-based line with is_omitted=false but "
            f"employee_external_id={emp_s!r} is not a roll id — use attendance_records or emit omitted."
        )
    if not issues:
        return None
    return (
        "Attendance-based rate line(s) billed without a real roll employee_external_id:\n"
        + "\n".join(f"- {x}" for x in issues)
        + "\n\nRe-emit the **full** MIS Summary JSON. For **rate_attendance** and **per_visit** with "
        "**attendance_required** ≠ false, **is_omitted=false** requires **employee_external_id** ∈ "
        "**attendance_records**; otherwise **is_omitted=true**, **final_amount=0**, **omit_reason**."
    )


def _mis_headcount_kept_zero_anomaly_message(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> str | None:
    """
    After server merges, detect staffing billing inconsistencies: non-omitted `final_amount=0`
    on roll-tied staffing lines (same predicate as coerce/duplicate: ``_mis_rate_line_is_attendance_based``)
    while peers on the same ``contract_rate_line_id`` are non-omitted with positive amounts, or a
    **sole** kept ``per_visit`` row at zero with positive ``rate_amount`` (typical error on monthly
    physician or per-seat ``per_visit`` lines). Triggers one LLM self-correction retry for every
    ``billing_profile`` (``taco``, ``tcs``, ``generic``).
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return None
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    by_crl: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        emp = rr.get("employee_external_id")
        if not crl or emp is None or not str(emp).strip():
            continue
        emp_s = str(emp).strip()
        if emp_s == MIS_SERVICE_CHARGE_EXTERNAL_ID or emp_s == NON_EMPLOYEE_SENTINEL:
            continue
        by_crl[crl].append(rr)

    issues: list[str] = []
    for crl_id, group in by_crl.items():
        rl = rl_by_id.get(crl_id)
        if not rl or not _mis_rate_line_is_attendance_based(rl):
            continue
        bm = str(rl.get("billing_model") or "").lower().strip()
        ra = _decimal(rl.get("rate_amount"))
        if ra <= 0:
            continue
        non_om = [r for r in group if not bool(r.get("is_omitted"))]
        if not non_om:
            continue
        amounts = [_decimal(r.get("final_amount")) for r in non_om]
        if len(non_om) >= 2 and max(amounts) > 0 and any(a == 0 for a in amounts):
            bad = [
                f"{r.get('employee_external_id')} ({r.get('employee_name') or ''})".strip()
                for r in non_om
                if _decimal(r.get("final_amount")) == 0
            ]
            if bad:
                issues.append(
                    f"contract_rate_line_id={crl_id}: non-omitted row(s) with final_amount=0: {', '.join(bad)} "
                    f"while other non-omitted peer(s) on this line have final_amount>0."
                )
        elif len(non_om) == 1 and bm == "per_visit" and amounts[0] == 0:
            r0 = non_om[0]
            issues.append(
                f"contract_rate_line_id={crl_id}: sole non-omitted `per_visit` staffing row has "
                f"final_amount=0 but rate_amount>0 (employee_external_id={r0.get('employee_external_id')!r})."
            )
    if not issues:
        return None
    return (
        "Staffing billing inconsistency detected after server merge:\n"
        + "\n".join(f"- {x}" for x in issues)
        + "\n\nRe-emit the **full** MIS Summary JSON for this same site and period. Rules:\n"
        "- For every roll-tied staffing row (`rate_attendance` / `per_visit` with `attendance_required` "
        "≠ false) with `is_omitted=false`, `final_amount` MUST equal the amount required by **your MIS user "
        "prompt for this site** (visits, sessions, MAP/calendar rules, per-line rates, and caps). "
        "**Never** leave `final_amount=0` on a kept row unless the contract formula truly yields zero.\n"
        "- Only rows with `is_omitted=true` from `contracted_quantity` cap (or routing) may use "
        "`final_amount=0` for that reason; **calc_notes** must show **pre-cap** amount + "
        "`is_omitted=true` for cap losers.\n"
        "- When several employees compete for the same line, apply **bill everyone eligible first**, then "
        "apply **contracted_quantity** and routing so losers are `is_omitted=true` with correct "
        "`omit_reason` and `calc_notes`, exactly as your MIS prompt describes."
    )


def _mis_contracted_quantity_underbill_anomaly_message(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> str | None:
    """
    Staffing lines: ``contracted_quantity`` = N but fewer than N ``is_omitted=false`` rows while
    at least N employee ``summary_rows`` exist for that ``contract_rate_line_id``.

    Catches LLM under-filling headcount (e.g. one billed nurse + one wrongly omitted when N=2 and
    two rows are present). Detection is **summary-rows scoped** — it does not count roll candidates
    unless they appear as rows (emitting rows is the LLM's responsibility).

    Triggers the same MIS self-correction retry path as other staffing anomalies.
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return None
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    by_crl: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        emp = rr.get("employee_external_id")
        if not crl or emp is None or not str(emp).strip():
            continue
        emp_s = str(emp).strip()
        if emp_s == MIS_SERVICE_CHARGE_EXTERNAL_ID or emp_s == NON_EMPLOYEE_SENTINEL:
            continue
        by_crl[crl].append(rr)

    issues: list[str] = []
    for crl_id, group in by_crl.items():
        rl = rl_by_id.get(crl_id)
        if not rl or not _mis_rate_line_is_attendance_based(rl):
            continue
        cq = rl.get("contracted_quantity")
        if cq is None:
            continue
        try:
            cap = int(_decimal(cq))
        except Exception:
            continue
        if cap < 1:
            continue
        m = len(group)
        k = sum(1 for r in group if not bool(r.get("is_omitted")))
        if m >= cap and k < cap:
            role = str(rl.get("role_code") or "").strip() or "?"
            issues.append(
                f"contract_rate_line_id={crl_id} (role_code={role}): contracted_quantity={cap} "
                f"but only {k} non-omitted employee row(s) while {m} employee row(s) exist — "
                f"bill up to {cap} distinct employees per §8 (paid_days, then final_amount, then id) "
                f"unless routing legitimately removes duplicates across lines."
            )
    if not issues:
        return None
    return (
        "Contract headcount under-fill detected after server merge:\n"
        + "\n".join(f"- {x}" for x in issues)
        + "\n\nRe-emit the **full** MIS Summary JSON. For each listed line, keep **up to** "
        "**contracted_quantity** non-omitted rows (top ranks per your MIS §8 / headcount cap). "
        "Do **not** omit eligible employees citing informal “staffed requirement” when the contract "
        "quantity allows more billings and enough candidate rows exist in **summary_rows**."
    )


def _mis_as_per_kept_zero_positive_rate_anomaly_message(
    summary_json: dict[str, Any],
    rate_lines: list[dict[str, Any]],
) -> str | None:
    """
    Non-employee ``as_per_actuals`` rows: kept but final_amount still 0 after server fallbacks,
    only when the **rate line** signals cap/fallback (billing_rules keys, or BMW-style contract text).
    Avoids false positives on equipment/medicines as_per lines with a nominal rate_amount. Does not
    parse ``calc_notes``. Uses the same LLM self-correction retry path as staffing, for every
    ``billing_profile`` (``taco``, ``tcs``, ``generic``).
    """
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return None
    rl_by_id = {str(rl.get("id")): rl for rl in rate_lines if rl.get("id")}
    bad_ids: list[str] = []
    for rr in rows:
        if not isinstance(rr, dict):
            continue
        emp = rr.get("employee_external_id")
        if emp is not None and str(emp).strip() and str(emp).strip() not in (
            NON_EMPLOYEE_SENTINEL,
            MIS_SERVICE_CHARGE_EXTERNAL_ID,
        ):
            continue
        if bool(rr.get("is_omitted")):
            continue
        if _decimal(rr.get("final_amount")) > 0:
            continue
        crl_id = str(rr.get("contract_rate_line_id") or "").strip()
        rl = rl_by_id.get(crl_id)
        if not rl or str(rl.get("billing_model") or "").lower().strip() != "as_per_actuals":
            continue
        if _decimal(rl.get("rate_amount")) <= 0:
            continue
        if not _as_per_contract_signals_cap_or_fallback_when_no_actuals(rl):
            continue
        bad_ids.append(crl_id or "?")
    if not bad_ids:
        return None
    return (
        "Non-employee `as_per_actuals` line(s) with `is_omitted=false` but `final_amount` still 0 "
        f"after server merge (contract_rate_line_id: {', '.join(bad_ids)}).\n\n"
        "Re-emit the **full** MIS Summary JSON. For **lower of (actual, cap)** / approximate monthly "
        "cap when **actuals_available=false** and no actual scalar is in the payload, bill the **cap** "
        "(typically `rate_amount`) per contract — **not** a conservative zero unless the contract "
        "explicitly forbids billing until proof. Align `calc_notes` with `final_amount`."
    )


# -----------------------------------------------------------------------------
# Post-LLM ``summary_json`` workflow (each new LLM response: first pass + self-correction retry)
#
#   1. ``_mis_post_llm_repair_employee_external_ids`` — fix ``summary_rows[].employee_external_id``
#      using ``attendance_records`` (unique name match, NA-158). The merge pipeline does **not**
#      repeat repair.
#   2. (First pass only) ``_mis_attendance_staffing_roll_id_anomaly_message`` — build text for the
#      retry appendix from the **step-1** JSON (same ids the pipeline will see).
#   3. ``_apply_post_llm_summary_pipeline`` — attendance coercion, cap, admin %, totals sync, …
#
# Retry: step 1 → step 3 only. ``parts`` for the single retry still use the step-2 string from
# the **first** LLM response.
# -----------------------------------------------------------------------------


def _mis_post_llm_repair_employee_external_ids(
    summary_json: dict[str, Any],
    attendance_records: list[dict[str, Any]],
) -> None:
    """Post-LLM **step 1** — see comment block immediately above."""
    _repair_summary_row_employee_external_ids(summary_json, attendance_records)


def _apply_post_llm_summary_pipeline(
    summary_json: dict[str, Any],
    *,
    att_metrics: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    billing_profile_norm: str,
    client_site_key: str,
    service_site_id: str,
) -> None:
    """
    Post-LLM **step 3** — merge / normalize pipeline (coerce, cap, invoice admin, totals, …).

    **Prerequisite:** ``_mis_post_llm_repair_employee_external_ids(summary_json, att_metrics)``
    has just been run on this object (and step 2 snapshot taken when applicable).
    """
    _coerce_attendance_staffing_rows_without_roll_id_inplace(summary_json, lines, att_metrics)
    _apply_contract_defaults_to_summary_rows(summary_json, lines)
    _apply_display_role_labels(summary_json, lines, att_metrics)
    _apply_non_employee_fixed_final_defaults(summary_json, lines)
    _apply_as_per_actuals_fallback_from_contract(summary_json, lines)
    _cap_employee_summary_rows_by_contracted_quantity(summary_json, att_metrics, lines)
    _apply_shared_resource_primary_only_adjustments(
        summary_json,
        lines,
        client_site_key=client_site_key,
        service_site_id=service_site_id,
    )
    _reconcile_invoice_admin_pct_rows_from_staffing(summary_json, lines)
    if billing_profile_norm == BILLING_PROFILE_GENERIC:
        _apply_calc_notes_amount_to_summary_rows(summary_json)
    _quantize_mis_summary_final_amounts_currency(summary_json)
    _sync_mis_summary_totals(summary_json)


def _service_site_row_recency_key(r: dict[str, Any]) -> tuple[datetime, str]:
    """Larger key = newer row (tie-break duplicate plants that all bill for the period)."""
    ts = r.get("created_at")
    if isinstance(ts, datetime):
        tsv = ts.replace(tzinfo=None) if ts.tzinfo else ts
    else:
        tsv = datetime.min
    return (tsv, str(r.get("service_site_id") or ""))


def _dedupe_site_rows_by_id(rows: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        d = dict(row)
        sid = str(d["service_site_id"])
        if sid not in seen:
            seen.add(sid)
            out.append(d)
    return out


async def _count_approved_billable_terms_for_ambiguity_async(
    session: AsyncSession,
    billing_client_id: str,
    service_site_id: str,
    *,
    period_start: date,
    period_end: date,
) -> int:
    from app.agents.o2c_ohc.terms_picker import count_approved_billable_terms_async

    return await count_approved_billable_terms_async(
        session,
        billing_client_id,
        service_site_id,
        period_start=period_start,
        period_end=period_end,
    )


async def _pick_terms_version_async(
    session: AsyncSession,
    billing_client_id: str,
    service_site_id: str,
    *,
    period_start: date,
    period_end: date,
    allow_expired_contract_terms: bool = False,
) -> dict[str, Any] | None:
    from app.agents.o2c_ohc.terms_picker import list_billable_terms_for_site_async, pick_from_billable_rows

    rows = await list_billable_terms_for_site_async(
        session,
        billing_client_id,
        service_site_id,
        period_start=period_start,
        period_end=period_end,
    )
    picked = pick_from_billable_rows(rows)
    if picked is not None:
        return picked

    approved_n = sum(1 for r in rows if str(r.get("status") or "").lower() == "approved")
    if approved_n >= 2:
        return None

    # Opt-in fallback for manual/recon reruns: allow selecting non-rejected terms
    # even when the billable window/status filters do not match the period.
    if not allow_expired_contract_terms:
        return None

    from app.agents.o2c_ohc.terms_picker import count_approved_terms_for_site_async

    if await count_approved_terms_for_site_async(session, billing_client_id, service_site_id) >= 2:
        return None

    r_fallback = await session.execute(
        text("""
        SELECT ctv.id AS contract_terms_version_id, ctv.billing_client_id, ctv.status,
               COALESCE(ctv.billing_profile, 'generic') AS billing_profile
        FROM contract_terms_version ctv
        WHERE ctv.billing_client_id = CAST(:bc AS uuid)
          AND ctv.status <> 'rejected'
          AND ctv.superseded_by_id IS NULL
          AND EXISTS (
            SELECT 1 FROM contract_rate_line crl
            WHERE crl.contract_terms_version_id = ctv.id
              AND crl.is_active = true
              AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
          )
        ORDER BY
          CASE WHEN ctv.effective_to IS NULL THEN 1 ELSE 0 END,
          ctv.effective_to DESC NULLS LAST,
          ctv.created_at DESC
        LIMIT 1
        """),
        {
            "bc": billing_client_id,
            "ssid": service_site_id,
        },
    )
    row = r_fallback.mappings().first()
    return dict(row) if row else None


async def _unique_billable_site_for_period_async(
    session: AsyncSession,
    candidate_rows: list[dict[str, Any]],
    *,
    period_start: date | None,
    period_end: date | None,
    allow_expired_contract_terms: bool = False,
) -> dict[str, Any] | None:
    if not candidate_rows:
        return None
    if len(candidate_rows) == 1:
        return candidate_rows[0]
    if period_start is None or period_end is None:
        return None
    winners: list[dict[str, Any]] = []
    for r in candidate_rows:
        bc = str(r["billing_client_id"])
        ss = str(r["service_site_id"])
        if await _pick_terms_version_async(
            session,
            bc,
            ss,
            period_start=period_start,
            period_end=period_end,
            allow_expired_contract_terms=allow_expired_contract_terms,
        ):
            winners.append(r)
    if len(winners) == 1:
        return winners[0]
    if len(winners) > 1:
        return max(winners, key=_service_site_row_recency_key)
    return None


async def _resolve_site_async(
    session: AsyncSession,
    client_site_key: str,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
    allow_expired_contract_terms: bool = False,
) -> dict[str, Any] | None:
    k = (client_site_key or "").strip()
    if not k:
        return None
    r = await session.execute(
        text("""
        SELECT ss.id AS service_site_id,
               ss.billing_client_id,
               ss.display_name,
               ss.canonical_name,
               ss.created_at
        FROM service_site ss
        JOIN site_alias sa ON sa.service_site_id = ss.id
        WHERE sa.alias_code = :ac
          AND sa.source_system IN (
            'ohc_excel', 'ohc_summary', 'truein_excel', 'truein', 'client_hr', 'erp', 'manual', 'ohc_llm_match'
          )
        ORDER BY
          CASE sa.source_system
            WHEN 'manual' THEN 3
            WHEN 'ohc_llm_match' THEN 2
            ELSE 1
          END DESC,
          sa.verified_at DESC NULLS LAST,
          ss.id
        """),
        {"ac": k},
    )
    alias_rows = _dedupe_site_rows_by_id([dict(row) for row in r.mappings().all()])
    picked = await _unique_billable_site_for_period_async(
        session,
        alias_rows,
        period_start=period_start,
        period_end=period_end,
        allow_expired_contract_terms=allow_expired_contract_terms,
    )
    if picked is not None:
        return picked
    if alias_rows:
        return None

    r2 = await session.execute(
        text("""
        SELECT id AS service_site_id, billing_client_id, display_name, canonical_name, created_at
        FROM service_site
        WHERE display_name = :dn
        ORDER BY created_at ASC
        """),
        {"dn": k},
    )
    disp_rows = [dict(row) for row in r2.mappings().all()]
    picked = await _unique_billable_site_for_period_async(
        session,
        disp_rows,
        period_start=period_start,
        period_end=period_end,
        allow_expired_contract_terms=allow_expired_contract_terms,
    )
    if picked is not None:
        return picked
    if len(disp_rows) == 1:
        return disp_rows[0]
    if disp_rows:
        return None

    r3 = await session.execute(
        text("""
        SELECT id AS service_site_id, billing_client_id, display_name, canonical_name, created_at
        FROM service_site
        WHERE lower(canonical_name) = lower(:cn)
        ORDER BY created_at ASC
        """),
        {"cn": k},
    )
    canon_rows = [dict(row) for row in r3.mappings().all()]
    picked = await _unique_billable_site_for_period_async(
        session,
        canon_rows,
        period_start=period_start,
        period_end=period_end,
        allow_expired_contract_terms=allow_expired_contract_terms,
    )
    if picked is not None:
        return picked
    if len(canon_rows) == 1:
        return canon_rows[0]
    return None


async def _rate_lines_for_site_async(
    session: AsyncSession, terms_version_id: str, service_site_id: str
) -> list[dict[str, Any]]:
    skip_global = skip_global_crl_when_site_override_exists_sql(crl_alias="contract_rate_line")
    r = await session.execute(
        text(f"""
        SELECT id, billing_model, role_code, description, rate_amount, rate_unit,
               contracted_quantity, actuals_markup_pct, attendance_required, minimum_units_per_period,
               unfilled_penalty_pct, ot_multiplier, schedule_type, schedule_config, currency,
               service_charge_type, service_charge_value,
               billing_rules, billing_rule_text, source_ref, model_config,
               service_site_id
        FROM contract_rate_line
        WHERE contract_terms_version_id = CAST(:tv AS uuid)
          AND is_active = true
          AND (service_site_id = CAST(:ssid AS uuid) OR service_site_id IS NULL)
          {skip_global}
        ORDER BY billing_model, role_code, id
        """),
        {"tv": terms_version_id, "ssid": service_site_id},
    )
    return [dict(row) for row in r.mappings().all()]


async def _o2c_drive_meta_for_terms_version_async(
    session: AsyncSession, tv_id: str
) -> dict[str, Any] | None:
    r = await session.execute(
        text("""
        SELECT cd.id::text AS id, cd.folder_path
        FROM contract_document cd
        INNER JOIN contract_terms_document ctd ON ctd.contract_document_id = cd.id
        WHERE ctd.contract_terms_version_id = CAST(:tv AS uuid)
        ORDER BY cd.created_at DESC NULLS LAST, cd.id DESC
        LIMIT 1
        """),
        {"tv": tv_id},
    )
    row = r.mappings().first()
    if not row:
        return None
    d = dict(row)
    fp = (d.get("folder_path") or "").strip()
    seg = contract_folder_segment_for_mis(fp)
    return {
        "contract_document_id": d["id"],
        "contract_folder_segment": seg,
        "contract_folder_path": fp,
    }


@dataclass
class MisDraftResult:
    status: str
    mis_run_id: str | None
    client_site_key: str
    reason: str = ""
    xlsx_path: str | None = None


def _build_mis_xlsx_to_path(
    mr: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    out_dir: Path,
    template_path: Path,
) -> Path:
    """Build workbook from in-memory MIS run + summary rows (CPU/OpenPyXL; run in threadpool from async)."""
    _ = template_path
    mr = dict(mr)
    dj = mr.get("detailed_json")
    if isinstance(dj, str):
        try:
            mr["detailed_json"] = json.loads(dj)
        except json.JSONDecodeError:
            mr["detailed_json"] = None

    final_total = float(sum(_decimal(r.get("final_amount")) for r in rows).quantize(Decimal("0.01")))
    rows_for_export = list(rows) + [
        {
            "description": "total",
            "contractual_rate": None,
            "contracted_count": None,
            "absent_days": None,
            "final_amount": final_total,
            "comments": "Auto-summed total",
            "employee_name": None,
        }
    ]

    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "OHC Summary"
    ws_det = wb.create_sheet("OHC Detailed")

    headers = ["Service", "Contracted Rate", "Contracted Count", "Absent Days", "Final Amount", "Comments"]
    for c, h in enumerate(headers, start=1):
        cell = ws_sum.cell(1, c, h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(fill_type="solid", fgColor="FFD9D9D9")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws_sum.freeze_panes = "A2"
    ws_sum.column_dimensions["A"].width = 30
    ws_sum.column_dimensions["B"].width = 16
    ws_sum.column_dimensions["C"].width = 16
    ws_sum.column_dimensions["D"].width = 12
    ws_sum.column_dimensions["E"].width = 14
    ws_sum.column_dimensions["F"].width = 64

    for i, row in enumerate(rows_for_export, start=2):
        ws_sum.cell(i, 1).value = row.get("description") or ""
        ws_sum.cell(i, 2).value = float(row.get("contractual_rate") or 0) if row.get("contractual_rate") is not None else None
        ws_sum.cell(i, 3).value = float(row.get("contracted_count") or 0) if row.get("contracted_count") is not None else None
        ws_sum.cell(i, 4).value = float(row.get("absent_days") or 0) if row.get("absent_days") is not None else None
        ws_sum.cell(i, 5).value = float(row.get("final_amount") or 0)
        ws_sum.cell(i, 6).value = row.get("comments") or row.get("employee_name") or ""
        if i == len(rows_for_export) + 1:
            ws_sum.cell(i, 1).font = Font(bold=True)
            ws_sum.cell(i, 5).font = Font(bold=True)

    det = mr.get("detailed_json")
    if isinstance(det, dict) and det.get("ok") and isinstance(det.get("dates"), list) and isinstance(det.get("employees"), list):
        dates = [str(x) for x in det.get("dates") or []]
        emps = det.get("employees") or []
        grid = det.get("grid") or {}

        ws_det.cell(2, 1).value = "Date"
        ws_det.cell(2, 1).font = Font(bold=True)
        ws_det.column_dimensions["A"].width = 18

        for j, e in enumerate(emps, start=2):
            role = e.get("role") or ""
            name = e.get("name") or ""
            ws_det.cell(1, j).value = role
            ws_det.cell(2, j).value = name
            ws_det.cell(1, j).font = Font(bold=True)
            ws_det.cell(2, j).font = Font(bold=True)
            ws_det.column_dimensions[ws_det.cell(1, j).column_letter].width = 24

        def _fmt_day(ds: str) -> str:
            try:
                d = date.fromisoformat(ds)
                return f"{d.day}-{d.strftime('%b-%y')} , {d.strftime('%a')}"
            except Exception:
                return ds

        def _status_fill(v: Any) -> PatternFill | None:
            s = str(v or "").strip().lower()
            if not s:
                return None
            if "present" in s:
                return PatternFill(fill_type="solid", fgColor="FFB6D7A8")
            if "locum duty" in s:
                return PatternFill(fill_type="solid", fgColor="FF00FF00")
            if "week off" in s or s == "weekoff":
                return PatternFill(fill_type="solid", fgColor="FFFFFF00")
            if "leave" in s:
                return PatternFill(fill_type="solid", fgColor="FFEA9999")
            if "absent" in s:
                return PatternFill(fill_type="solid", fgColor="FFFF0000")
            if "holiday" in s:
                return PatternFill(fill_type="solid", fgColor="FF3D85C6")
            return None

        for i, ds in enumerate(dates, start=3):
            ws_det.cell(i, 1).value = _fmt_day(ds)
            for j, e in enumerate(emps, start=2):
                nm = e.get("name") or ""
                v = (grid.get(nm) or {}).get(ds)
                c = ws_det.cell(i, j)
                c.value = v
                c.alignment = Alignment(horizontal="center", vertical="center")
                fill = _status_fill(v)
                if fill is not None:
                    c.fill = fill
    ws_det.freeze_panes = "B3"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = (
        f"mis_{_safe_file_part(mr.get('client_site_key'))}_"
        f"{_safe_file_part(mr.get('billing_period_start'))}_"
        f"{_safe_file_part(mr.get('billing_period_end'))}.xlsx"
    )
    out_path = out_dir / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    wb.close()
    return out_path


async def _write_mis_xlsx_from_db_async(mis_run_id: str, *, out_dir: Path, template_path: Path) -> Path:
    """Export per-site MIS xlsx from DB rows (async load + OpenPyXL in threadpool)."""
    mr_d, rows = await fetch_mis_run_xlsx_bundle(mis_run_id)
    return await run_blocking(
        lambda: _build_mis_xlsx_to_path(mr_d, rows, out_dir=out_dir, template_path=template_path)
    )


def _write_mis_xlsx_from_db(mis_run_id: str, *, out_dir: Path, template_path: Path) -> Path:
    """Sync entrypoint when no running event loop (e.g. scripts)."""
    return run_agenos_async(_write_mis_xlsx_from_db_async(mis_run_id, out_dir=out_dir, template_path=template_path))


async def _update_mis_run_xlsx_path_async(mis_run_id: str, out_path: Path) -> None:
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE o2c_mis_run SET xlsx_path = :xp, updated_at = now() WHERE id = CAST(:id AS uuid)"
                ),
                {"xp": str(out_path), "id": mis_run_id},
            )


async def _approved_mis_exists_for_site_and_key_any_period_async(
    session: AsyncSession,
    *,
    service_site_id: str,
    client_site_key: str,
) -> bool:
    """True if any approved MIS run exists for this service site and attendance label (any billing period)."""
    k = (client_site_key or "").strip()
    if not k:
        return False
    r = await session.execute(
        text("""
        SELECT 1
        FROM o2c_mis_run
        WHERE service_site_id = CAST(:ssid AS uuid)
          AND client_site_key = :csk
          AND status = 'approved'
        LIMIT 1
        """),
        {"ssid": service_site_id, "csk": k},
    )
    return r.mappings().first() is not None


async def create_or_refresh_mis_draft_for_site_async(
    *,
    client_site_key: str,
    attendance_records: list[dict[str, Any]],
    detailed_json: dict[str, Any] | None,
    period_start: date,
    period_end: date,
    out_dir: Path,
    template_path: Path,
    allow_expired_contract_terms: bool = False,
    attendance_workbook_path: Path | str | None = None,
) -> MisDraftResult:
    key = (client_site_key or "").strip()
    records = list(attendance_records)

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            site = await _resolve_site_async(
                session,
                key,
                period_start=period_start,
                period_end=period_end,
                allow_expired_contract_terms=allow_expired_contract_terms,
            )
            llm_attempted = False
            if not site and settings.o2c_llm_site_match_enabled and (settings.openai_api_key or "").strip():
                llm_attempted = True
                if await try_llm_site_match_and_insert_alias_async(
                    session, key, period_start=period_start, period_end=period_end
                ):
                    site = await _resolve_site_async(
                        session,
                        key,
                        period_start=period_start,
                        period_end=period_end,
                        allow_expired_contract_terms=allow_expired_contract_terms,
                    )
            if not site:
                await persist_o2c_attendance_site_recon_async(
                    [O2cAttendanceSiteSkip(client_site_key=key, reason_code="unresolved_site", detail="No service_site mapping", attendance_row_count=len(records), llm_match_attempted=llm_attempted)],
                    period_start=period_start,
                    period_end=period_end,
                )
                return MisDraftResult(status="skipped", mis_run_id=None, client_site_key=key, reason="unresolved_site")

            bc_id = str(site["billing_client_id"])
            ss_id = str(site["service_site_id"])
            tv = await _pick_terms_version_async(
                session,
                bc_id,
                ss_id,
                period_start=period_start,
                period_end=period_end,
                allow_expired_contract_terms=allow_expired_contract_terms,
            )
            if not tv:
                n_amb = await _count_approved_billable_terms_for_ambiguity_async(
                    session,
                    bc_id,
                    ss_id,
                    period_start=period_start,
                    period_end=period_end,
                )
                if n_amb > 1:
                    await persist_o2c_attendance_site_recon_async(
                        [
                            O2cAttendanceSiteSkip(
                                client_site_key=key,
                                reason_code="terms_ambiguous",
                                detail=f"{n_amb} approved contract_terms_version rows overlap this period",
                                attendance_row_count=len(records),
                                llm_match_attempted=llm_attempted,
                            )
                        ],
                        period_start=period_start,
                        period_end=period_end,
                    )
                    return MisDraftResult(
                        status="skipped",
                        mis_run_id=None,
                        client_site_key=key,
                        reason="terms_ambiguous",
                    )
                alias_src = await winning_site_alias_source_async(session, key)
                if alias_src == "manual":
                    await persist_o2c_attendance_site_recon_async(
                        [
                            O2cAttendanceSiteSkip(
                                client_site_key=key,
                                reason_code="manual_mapping_no_billable_contract",
                                detail=(
                                    "Manual site_alias maps to a site with no billable contract for this period; "
                                    "fix contract dates/status or remap manually."
                                ),
                                attendance_row_count=len(records),
                                llm_match_attempted=llm_attempted,
                            )
                        ],
                        period_start=period_start,
                        period_end=period_end,
                    )
                    return MisDraftResult(
                        status="skipped",
                        mis_run_id=None,
                        client_site_key=key,
                        reason="manual_mapping_no_billable_contract",
                    )
                approved_lock = await _approved_mis_exists_for_site_and_key_any_period_async(
                    session,
                    service_site_id=ss_id,
                    client_site_key=key,
                )
                if (
                    not approved_lock
                    and settings.o2c_llm_site_match_enabled
                    and (settings.openai_api_key or "").strip()
                ):
                    if alias_src == SOURCE_LLM:
                        removed = await delete_ohc_llm_match_aliases_for_label_async(session, key)
                        if removed:
                            log.info(
                                "MIS: dropped %s stale LLM site_alias row(s) for %r (no billable terms for period)",
                                removed,
                                key[:80],
                            )
                    llm_attempted = True
                    if await try_llm_site_match_and_insert_alias_async(
                        session, key, period_start=period_start, period_end=period_end
                    ):
                        site = await _resolve_site_async(
                            session,
                            key,
                            period_start=period_start,
                            period_end=period_end,
                            allow_expired_contract_terms=allow_expired_contract_terms,
                        )
                        if site:
                            bc_id = str(site["billing_client_id"])
                            ss_id = str(site["service_site_id"])
                            tv = await _pick_terms_version_async(
                                session,
                                bc_id,
                                ss_id,
                                period_start=period_start,
                                period_end=period_end,
                                allow_expired_contract_terms=allow_expired_contract_terms,
                            )
                if not tv:
                    reason_code = "no_billable_contract"
                    detail = "No billable contract_terms_version"
                    n = await _count_approved_billable_terms_for_ambiguity_async(
                        session,
                        bc_id,
                        ss_id,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    if n > 1:
                        reason_code = "terms_ambiguous"
                        detail = f"{n} approved contract_terms_version rows overlap this period"
                    await persist_o2c_attendance_site_recon_async(
                        [
                            O2cAttendanceSiteSkip(
                                client_site_key=key,
                                reason_code=reason_code,
                                detail=detail,
                                attendance_row_count=len(records),
                                llm_match_attempted=llm_attempted,
                            )
                        ],
                        period_start=period_start,
                        period_end=period_end,
                    )
                    return MisDraftResult(
                        status="skipped",
                        mis_run_id=None,
                        client_site_key=key,
                        reason=reason_code,
                    )
            tv_id = str(tv["contract_terms_version_id"])
            lines = await _rate_lines_for_site_async(session, tv_id, ss_id)
            if not lines:
                await persist_o2c_attendance_site_recon_async(
                    [O2cAttendanceSiteSkip(client_site_key=key, reason_code="no_rate_lines", detail="Contract found but no applicable rate lines for this site", attendance_row_count=len(records), llm_match_attempted=False)],
                    period_start=period_start,
                    period_end=period_end,
                )
                return MisDraftResult(status="skipped", mis_run_id=None, client_site_key=key, reason="no_rate_lines")

            ins_mr = await session.execute(
                text("""
                INSERT INTO o2c_mis_run (
                    id, billing_client_id, service_site_id, contract_terms_version_id,
                    client_site_key, billing_period_start, billing_period_end,
                    status, template_path, xlsx_path, detailed_json, summary_json, notes, created_at, updated_at
                ) VALUES (
                    gen_random_uuid(), CAST(:bc AS uuid), CAST(:ssid AS uuid), CAST(:tv AS uuid), :csk, :ps, :pe,
                    'pending_human', :tpl, NULL, NULL, NULL, NULL, now(), now()
                )
                ON CONFLICT (service_site_id, billing_period_start, billing_period_end) DO UPDATE SET
                    billing_client_id = CASE
                        WHEN o2c_mis_run.status = 'approved' THEN o2c_mis_run.billing_client_id
                        ELSE EXCLUDED.billing_client_id
                    END,
                    contract_terms_version_id = CASE
                        WHEN o2c_mis_run.status = 'approved' THEN o2c_mis_run.contract_terms_version_id
                        ELSE EXCLUDED.contract_terms_version_id
                    END,
                    client_site_key = CASE
                        WHEN o2c_mis_run.status = 'approved' THEN o2c_mis_run.client_site_key
                        ELSE EXCLUDED.client_site_key
                    END,
                    template_path = CASE
                        WHEN o2c_mis_run.status = 'approved' THEN o2c_mis_run.template_path
                        ELSE EXCLUDED.template_path
                    END,
                    updated_at = CASE
                        WHEN o2c_mis_run.status = 'approved' THEN o2c_mis_run.updated_at
                        ELSE now()
                    END,
                    status = CASE
                        WHEN o2c_mis_run.status = 'approved' THEN 'approved'
                        WHEN o2c_mis_run.status = 'rejected' THEN 'rejected'
                        ELSE 'pending_human'
                    END
                RETURNING id, status
                """),
                {
                    "bc": bc_id,
                    "ssid": ss_id,
                    "tv": tv_id,
                    "csk": key,
                    "ps": period_start,
                    "pe": period_end,
                    "tpl": str(template_path),
                },
            )
            mr = ins_mr.mappings().first()
            mis_run_id = str(mr["id"]) if mr else None
            if not mis_run_id:
                return MisDraftResult(status="failed", mis_run_id=None, client_site_key=key, reason="mis_run_insert_failed")

            if str(mr.get("status") or "").strip().lower() == "approved":
                return MisDraftResult(
                    status="skipped",
                    mis_run_id=mis_run_id,
                    client_site_key=key,
                    reason="mis_already_approved",
                )

            absent_locks = await fetch_absent_days_locks_session(session, mis_run_id)

            if str(mr.get("status") or "").strip().lower() != "approved":
                await session.execute(
                    text("DELETE FROM o2c_mis_summary_row WHERE mis_run_id = CAST(:mid AS uuid)"),
                    {"mid": mis_run_id},
                )

            if getattr(settings, "o2c_manpower_add_missing_roll_rows", True):
                mp_path: Path | None = None
                wb_hint = (attendance_workbook_path or "").strip() if attendance_workbook_path else ""
                if wb_hint:
                    p = Path(wb_hint).expanduser()
                    if p.is_file():
                        mp_path = p.resolve()
                if mp_path is None:
                    from app.agents.o2c_ohc.mis_gdrive_finalize import (
                        build_mis_drive_svc,
                        resolve_attendance_xlsx_path,
                        unlink_o2c_temp_paths,
                    )

                    mp_cleanup: list[Path] = []
                    try:
                        resolved = resolve_attendance_xlsx_path(
                            explicit=None,
                            cleanup_paths=mp_cleanup,
                            drive_svc=build_mis_drive_svc(),
                        )
                        if resolved:
                            mp_path = Path(resolved).resolve()
                    finally:
                        unlink_o2c_temp_paths(mp_cleanup)
                if mp_path is not None:
                    records = append_manpower_period_churn_rows(
                        records,
                        mp_path,
                        key,
                        period_start=period_start,
                        period_end=period_end,
                    )

            att_metrics = _attendance_records_for_period(
                records,
                period_start=period_start,
                period_end=period_end,
            )
            det = detailed_json or build_detailed_json_from_records(
                key,
                records,
                period_start=period_start,
                period_end=period_end,
            )
            drive_meta = await _o2c_drive_meta_for_terms_version_async(session, tv_id)
            if drive_meta:
                det = {**det, "_o2c_drive": drive_meta}
            if not att_metrics:
                return MisDraftResult(status="failed", mis_run_id=mis_run_id, client_site_key=key, reason="attendance_roll_metrics_missing")

            _apply_fmo_mo_attendance_hints(att_metrics, lines)
            _flag_fmo_contract_line_missing_vs_attendance(att_metrics, lines)

            handover_prep = apply_handover_attendance_prep(
                att_metrics,
                lines,
                period_start=period_start,
                period_end=period_end,
            )
            att_metrics = handover_prep.records
            handover_meta = handover_prep.handover

            if absent_locks:
                patched = apply_absent_days_locks_to_attendance_records(att_metrics, absent_locks)
                if patched:
                    log.info(
                        "MIS rerun: applied %s human absent_days override(s) to attendance for site=%r run=%s",
                        patched,
                        key,
                        mis_run_id,
                    )

            wd = _working_days_inclusive(period_start, period_end)

            past_corr = await get_past_corrections_for_site(uuid.UUID(ss_id), limit=5)

            bp_norm = normalize_billing_profile(str(tv.get("billing_profile") or "generic"))

            summary_json = await llm_generate_mis_summary_json_async(
                client_site_key=key,
                period_start=period_start,
                period_end=period_end,
                working_days=int(wd),
                attendance_records=att_metrics,
                rate_lines=lines,
                past_corrections=past_corr if past_corr else None,
                billing_profile=str(tv.get("billing_profile") or "generic"),
            )
            if not summary_json:
                return MisDraftResult(status="failed", mis_run_id=mis_run_id, client_site_key=key, reason="llm_summary_failed")

            # Post-LLM steps 1 → 2 → 3 (see comment above _apply_post_llm_summary_pipeline).
            _mis_post_llm_repair_employee_external_ids(summary_json, att_metrics)
            attendance_roll_id_retry = _mis_attendance_staffing_roll_id_anomaly_message(
                summary_json, lines, att_metrics
            )
            _apply_post_llm_summary_pipeline(
                summary_json,
                att_metrics=att_metrics,
                lines=lines,
                billing_profile_norm=bp_norm,
                client_site_key=key,
                service_site_id=ss_id,
            )
            apply_handover_summary_row_fixup(summary_json, handover_meta)
            if handover_meta.get("applied"):
                _sync_mis_summary_totals(summary_json)

            parts = [
                _mis_duplicate_employee_staffing_anomaly_message(summary_json, lines),
                attendance_roll_id_retry,
                _mis_headcount_kept_zero_anomaly_message(summary_json, lines),
                _mis_contracted_quantity_underbill_anomaly_message(summary_json, lines),
                _mis_as_per_kept_zero_positive_rate_anomaly_message(summary_json, lines),
            ]
            retry_hint = "\n\n---\n\n".join(p for p in parts if p) or None
            if retry_hint:
                log.info(
                    "MIS LLM self-correction retry (duplicate staffing / attendance roll id / kept row billing / cq underbill / as_per cap) site=%r",
                    key,
                )
                summary_json2 = await llm_generate_mis_summary_json_async(
                    client_site_key=key,
                    period_start=period_start,
                    period_end=period_end,
                    working_days=int(wd),
                    attendance_records=att_metrics,
                    rate_lines=lines,
                    past_corrections=past_corr if past_corr else None,
                    billing_profile=str(tv.get("billing_profile") or "generic"),
                    model_self_correction_appendix=retry_hint,
                )
                if summary_json2:
                    summary_json = summary_json2
                    # Retry: post-LLM steps 1 → 3 only (attendance retry text is from first pass).
                    _mis_post_llm_repair_employee_external_ids(summary_json, att_metrics)
                    _apply_post_llm_summary_pipeline(
                        summary_json,
                        att_metrics=att_metrics,
                        lines=lines,
                        billing_profile_norm=bp_norm,
                        client_site_key=key,
                        service_site_id=ss_id,
                    )
                    apply_handover_summary_row_fixup(summary_json, handover_meta)
                    if handover_meta.get("applied"):
                        _sync_mis_summary_totals(summary_json)
                else:
                    log.warning(
                        "MIS LLM self-correction retry returned empty JSON; keeping first-pass summary site=%r",
                        key,
                    )
            if _mis_duplicate_employee_staffing_anomaly_message(summary_json, lines):
                log.warning(
                    "MIS summary_json still shows duplicate staffing (same employee on multiple "
                    "non-omitted staffing lines) after pipeline/retry site=%r",
                    key,
                )
            summary_json["handover"] = handover_meta
            await session.execute(
                text("UPDATE o2c_mis_run SET summary_json = CAST(:sj AS jsonb), updated_at = now() WHERE id = CAST(:mid AS uuid)"),
                {"sj": json.dumps(summary_json), "mid": mis_run_id},
            )

            rate_line_ids = {str(rl.get("id")) for rl in lines if rl.get("id")}
            emp_ids = {str(r.get("employee_external_id") or "") for r in att_metrics if r.get("employee_external_id")}

            sum_rows = summary_json.get("summary_rows")
            if not isinstance(sum_rows, list):
                return MisDraftResult(status="failed", mis_run_id=mis_run_id, client_site_key=key, reason="llm_summary_bad_shape")

            for rr in sum_rows:
                if not isinstance(rr, dict):
                    continue
                crl_id = str(rr.get("contract_rate_line_id") or "").strip()
                if not crl_id or crl_id not in rate_line_ids:
                    continue

                emp_id = rr.get("employee_external_id")
                emp_id_s = str(emp_id).strip() if emp_id is not None else ""
                emp_name = rr.get("employee_name")
                emp_name_s = str(emp_name).strip() if emp_name is not None else ""

                if not emp_id_s:
                    emp_id_s = NON_EMPLOYEE_SENTINEL
                    emp_name_s = ""
                elif emp_id_s == MIS_SERVICE_CHARGE_EXTERNAL_ID:
                    emp_name_s = ""
                else:
                    if emp_id_s not in emp_ids:
                        continue

                service = str(rr.get("service") or "").strip() or str(rr.get("role_code") or "").strip() or "line"
                role_code = rr.get("role_code")
                comments = rr.get("comments")
                omit = bool(rr.get("is_omitted") or False)
                omit_reason = rr.get("omit_reason")
                calc_notes = rr.get("calc_notes")

                def _num(x: Any) -> Decimal | None:
                    try:
                        return Decimal(str(x))
                    except Exception:
                        return None

                contracted_rate = _num(rr.get("contracted_rate"))
                contracted_count = _num(rr.get("contracted_count"))
                present_days = _num(rr.get("present_days"))
                absent_days = _num(rr.get("absent_days"))
                _fa = _num(rr.get("final_amount"))
                final_amount = _fa if _fa is not None else Decimal(0)

                await session.execute(
                    text("""
                    INSERT INTO o2c_mis_summary_row (
                        id, mis_run_id, contract_rate_line_id, role_code, description,
                        contractual_rate, contracted_count, total_days, attendance_days, absent_days, final_amount,
                        comments,
                        is_omitted, omit_reason, calc_notes,
                        employee_external_id, employee_name
                    ) VALUES (
                        gen_random_uuid(), CAST(:mid AS uuid), CAST(:crl AS uuid), :rc, :desc,
                        :crate, :ccount, :td, :ad, :abd, :fa,
                        :comments,
                        :omit, :oreason, :cnotes,
                        :eid, :ename
                    )
                    ON CONFLICT (mis_run_id, contract_rate_line_id, employee_external_id) DO UPDATE SET
                        role_code = EXCLUDED.role_code,
                        description = EXCLUDED.description,
                        contractual_rate = EXCLUDED.contractual_rate,
                        contracted_count = EXCLUDED.contracted_count,
                        total_days = EXCLUDED.total_days,
                        attendance_days = EXCLUDED.attendance_days,
                        absent_days = EXCLUDED.absent_days,
                        final_amount = EXCLUDED.final_amount,
                        comments = EXCLUDED.comments,
                        is_omitted = EXCLUDED.is_omitted,
                        omit_reason = EXCLUDED.omit_reason,
                        calc_notes = EXCLUDED.calc_notes,
                        employee_name = EXCLUDED.employee_name
                    """),
                    {
                        "mid": mis_run_id,
                        "crl": crl_id,
                        "rc": (str(role_code).strip()[:200] if role_code is not None else None),
                        "desc": service[:500],
                        "crate": contracted_rate,
                        "ccount": contracted_count,
                        "td": Decimal(int(wd)),
                        "ad": present_days,
                        "abd": absent_days,
                        "fa": final_amount,
                        "comments": (str(comments).strip()[:2000] if comments is not None else None),
                        "omit": omit,
                        "oreason": (str(omit_reason).strip()[:2000] if omit_reason is not None else None),
                        "cnotes": (str(calc_notes).strip()[:10000] if calc_notes is not None else None),
                        "eid": emp_id_s[:500],
                        "ename": emp_name_s[:500] if emp_name_s else None,
                    },
                )

            if absent_locks:
                await reapply_absent_days_locks_session(session, mis_run_id, absent_locks)

            await session.execute(
                text("UPDATE o2c_mis_run SET detailed_json = CAST(:dj AS jsonb), updated_at = now() WHERE id = CAST(:mid AS uuid)"),
                {"dj": json.dumps(det), "mid": mis_run_id},
            )

    out_path = await _write_mis_xlsx_from_db_async(mis_run_id, out_dir=out_dir, template_path=template_path)
    await _update_mis_run_xlsx_path_async(mis_run_id, out_path)

    cleared = await clear_open_o2c_attendance_site_recon_for_period_key_async(
        client_site_key=key,
        period_start=period_start,
        period_end=period_end,
    )
    if cleared:
        log.info(
            "Cleared %s stale open o2c_attendance_site_recon row(s) for %r period %s–%s after MIS ok",
            cleared,
            key,
            period_start,
            period_end,
        )

    return MisDraftResult(status="ok", mis_run_id=mis_run_id, client_site_key=key, xlsx_path=str(out_path))

