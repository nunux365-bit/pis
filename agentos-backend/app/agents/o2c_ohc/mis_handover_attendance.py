"""
LWD/DOJ handover prep for MIS draft: clip partial months and merge leaver→joiner on joiner row.

Conservative: only pairs 1:1 leaver/joiner per contract rate line when role mapping is unambiguous.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config.settings import settings

log = logging.getLogger(__name__)

_DATE_FORMATS = (
    "%d-%b-%y",
    "%d-%b-%Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d/%m/%y",
)


def parse_sheet_date(raw: Any, *, period_start: date | None = None) -> date | None:
    """Parse attendance sheet LWD/DOJ cells (e.g. ``15-Apr-26``)."""
    s = str(raw or "").strip()
    if not s or s.lower() in ("na", "n/a", "-", "—"):
        return None
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
            if fmt == "%d-%b-%y" and period_start and d.year < 100:
                # Two-digit year: anchor to billing period century.
                d = d.replace(year=period_start.year // 100 * 100 + (d.year % 100))
            return d
        except ValueError:
            continue
    return None


def _bucket_status(v: Any) -> str:
    if v is None:
        return "blank"
    s = str(v).strip()
    if not s:
        return "blank"
    sl = s.lower()
    if "half" in sl and "present" not in sl:
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


def _recompute_daywise_metrics(rec: dict[str, Any]) -> None:
    att_all = rec.get("ohc_attendance_daywise") or {}
    if not isinstance(att_all, dict):
        return
    counts = {
        "present_days": Decimal("0"),
        "absent_days": Decimal("0"),
        "week_off_days": Decimal("0"),
        "leave_days": Decimal("0"),
        "blank_days": Decimal("0"),
    }
    for v in att_all.values():
        b = _bucket_status(v)
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
    total_days = len(att_all)
    rec["present_days"] = counts["present_days"]
    rec["absent_days"] = counts["absent_days"]
    rec["week_off_days"] = float(counts["week_off_days"])
    rec["leave_days"] = float(counts["leave_days"])
    rec["blank_days"] = float(counts["blank_days"])
    rec["total_days"] = total_days
    rec["expected_days"] = float(max(Decimal("0"), Decimal(total_days) - counts["week_off_days"]))
    rec["ohc_attendance_days"] = total_days


def _clip_record_to_lwd_doj(
    rec: dict[str, Any],
    *,
    period_start: date,
    period_end: date,
) -> None:
    """Clip daywise grid to LWD (inclusive end for leaver) or DOJ (inclusive start for joiner)."""
    lwd = parse_sheet_date(rec.get("lwd"), period_start=period_start)
    doj = parse_sheet_date(rec.get("doj"), period_start=period_start)
    att_all = rec.get("ohc_attendance_daywise")
    if not isinstance(att_all, dict) or not att_all:
        return

    clipped: dict[str, Any] = {}
    for ds, st in att_all.items():
        try:
            d = date.fromisoformat(str(ds)[:10])
        except ValueError:
            continue
        if d < period_start or d > period_end:
            continue
        if lwd and d > lwd:
            clipped[ds] = None
            continue
        if doj and d < doj:
            clipped[ds] = None
            continue
        clipped[ds] = st if st not in ("",) else None
    rec["ohc_attendance_daywise"] = clipped
    _recompute_daywise_metrics(rec)


def _rate_line_is_staffing_attendance(rl: dict[str, Any]) -> bool:
    bm = str(rl.get("billing_model") or "").lower().strip()
    if bm not in ("rate_attendance", "per_visit"):
        return False
    return rl.get("attendance_required") is not False


def _norm_role_token(role: str) -> str:
    return re.sub(r"[\s_-]+", "", (role or "").strip().upper())


def _is_mo_family_crl_role(role_code: str) -> bool:
    rc = _norm_role_token(role_code)
    if not rc:
        return False
    if rc.startswith("FMO"):
        return False
    return (
        rc.startswith("MO")
        or rc.startswith("SRMO")
        or rc.startswith("JRMO")
        or "COMPANYMO" in rc
    )


def _narrow_churn_doctor_to_mo_line(
    rec: dict[str, Any],
    matches: list[dict[str, Any]],
    *,
    period_start: date | None,
    period_end: date | None,
) -> list[dict[str, Any]]:
    """Leaver/joiner rows on Doctor attendance: prefer sole MO-family CRL over FMO+MO ambiguity."""
    if len(matches) <= 1 or not period_start or not period_end:
        return matches
    lwd = parse_sheet_date(rec.get("lwd"), period_start=period_start)
    doj = parse_sheet_date(rec.get("doj"), period_start=period_start)
    in_period_lwd = lwd is not None and period_start <= lwd <= period_end
    in_period_doj = doj is not None and period_start <= doj <= period_end
    if not in_period_lwd and not in_period_doj:
        return matches
    mo_only = [rl for rl in matches if _is_mo_family_crl_role(str(rl.get("role_code") or ""))]
    if len(mo_only) == 1:
        return mo_only
    return matches


def attendance_maps_to_single_crl(
    rec: dict[str, Any],
    rate_lines: list[dict[str, Any]],
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any] | None:
    """
    Return the sole staffing CRL this attendance row may bill, or None if ambiguous / no match.

    When ``mis_staffing_fmo_role_code`` is set (FMO/MO hints from draft prep), only exact CRL
  role matches count — avoids billing a hinted MO row against both MO and FMO contract lines.
    """
    hint = str(rec.get("mis_staffing_fmo_role_code") or "").strip()
    if hint:
        hint_matches: list[dict[str, Any]] = []
        for rl in rate_lines:
            if not _rate_line_is_staffing_attendance(rl):
                continue
            crl_role = str(rl.get("role_code") or "").strip()
            if crl_role and _norm_role_token(hint) == _norm_role_token(crl_role):
                hint_matches.append(rl)
        if len(hint_matches) == 1:
            return hint_matches[0]
        return None

    att_role = str(rec.get("role_code") or "").strip()
    matches: list[dict[str, Any]] = []
    for rl in rate_lines:
        if not _rate_line_is_staffing_attendance(rl):
            continue
        crl_role = str(rl.get("role_code") or "").strip()
        if not crl_role:
            continue
        if att_role and _norm_role_token(att_role) == _norm_role_token(crl_role):
            matches.append(rl)
            continue
        ar = _norm_role_token(att_role)
        cr = _norm_role_token(crl_role)
        if not ar:
            continue
        if ar == "NURSE" and "NURSE" in cr:
            matches.append(rl)
            continue
        if ar == "DOCTOR" and (cr.startswith("MO") or cr.startswith("FMO") or "DOCTOR" in cr):
            matches.append(rl)
            continue
        if ar == "DRIVER" and "DRIVER" in cr:
            matches.append(rl)
            continue
    matches = _narrow_churn_doctor_to_mo_line(
        rec, matches, period_start=period_start, period_end=period_end
    )
    if len(matches) != 1:
        return None
    return matches[0]


@dataclass
class HandoverPair:
    crl_id: str
    leaver_id: str
    joiner_id: str
    leaver_name: str
    joiner_name: str


@dataclass
class HandoverPrepResult:
    records: list[dict[str, Any]]
    handover: dict[str, Any] = field(default_factory=dict)
    suppressed_by_crl: dict[str, list[str]] = field(default_factory=dict)


def _merge_daywise_handover(
    leaver: dict[str, Any],
    joiner: dict[str, Any],
    *,
    period_start: date,
    period_end: date,
    leaver_lwd: date,
    joiner_doj: date,
) -> dict[str, Any]:
    """Union leaver days through LWD and joiner days from DOJ; no double-count same calendar day."""
    lw = leaver.get("ohc_attendance_daywise") or {}
    jw = joiner.get("ohc_attendance_daywise") or {}
    if not isinstance(lw, dict):
        lw = {}
    if not isinstance(jw, dict):
        jw = {}

    merged: dict[str, Any] = {}
    cur = period_start
    while cur <= period_end:
        ds = cur.isoformat()
        if cur <= leaver_lwd:
            merged[ds] = lw.get(ds) if ds in lw else None
        elif cur >= joiner_doj:
            merged[ds] = jw.get(ds) if ds in jw else None
        else:
            merged[ds] = None
        cur += timedelta(days=1)

    for ds in set(list(lw.keys()) + list(jw.keys())):
        if ds in merged:
            continue
        try:
            d = date.fromisoformat(str(ds)[:10])
        except ValueError:
            continue
        if d < period_start or d > period_end:
            continue
        if d <= leaver_lwd:
            merged[ds] = lw.get(ds)
        elif d >= joiner_doj:
            merged[ds] = jw.get(ds)

    out = copy.deepcopy(joiner)
    out["ohc_attendance_daywise"] = {
        k: (v if v not in ("",) else None) for k, v in merged.items()
    }
    _recompute_daywise_metrics(out)
    return out


def _comma_names(*parts: str) -> str:
    seen: list[str] = []
    for p in parts:
        t = (p or "").strip()
        if t and t not in seen:
            seen.append(t)
    return ", ".join(seen)


def apply_handover_attendance_prep(
    attendance_records: list[dict[str, Any]],
    rate_lines: list[dict[str, Any]],
    *,
    period_start: date,
    period_end: date,
) -> HandoverPrepResult:
    """
    Clip LWD/DOJ for all rows; pair-merge leaver→joiner per CRL when conservative rules pass.
    """
    if not bool(getattr(settings, "o2c_mis_handover_pairing_enabled", True)):
        clipped = [dict(r) for r in attendance_records if isinstance(r, dict)]
        for rec in clipped:
            _clip_record_to_lwd_doj(rec, period_start=period_start, period_end=period_end)
        return HandoverPrepResult(records=clipped, handover={"applied": False, "reason": "disabled"})

    working = [dict(r) for r in attendance_records if isinstance(r, dict)]
    for rec in working:
        _clip_record_to_lwd_doj(rec, period_start=period_start, period_end=period_end)

    by_crl: dict[str, list[dict[str, Any]]] = {}
    unmapped: list[dict[str, Any]] = []
    for rec in working:
        if rec.get("_from_manpower_only"):
            lwd = parse_sheet_date(rec.get("lwd"), period_start=period_start)
            doj = parse_sheet_date(rec.get("doj"), period_start=period_start)
            in_period_lwd = lwd is not None and period_start <= lwd <= period_end
            in_period_doj = doj is not None and period_start <= doj <= period_end
            if not in_period_lwd and not in_period_doj:
                unmapped.append(rec)
                continue
        rl = attendance_maps_to_single_crl(
            rec, rate_lines, period_start=period_start, period_end=period_end
        )
        if not rl:
            unmapped.append(rec)
            continue
        cid = str(rl.get("id") or "")
        if not cid:
            unmapped.append(rec)
            continue
        by_crl.setdefault(cid, []).append(rec)

    suppressed: dict[str, list[str]] = {}
    pairs_out: list[dict[str, str]] = []
    remove_ids: set[str] = set()
    joiner_name_overrides: dict[str, str] = {}

    for rl in rate_lines:
        if not _rate_line_is_staffing_attendance(rl):
            continue
        crl_id = str(rl.get("id") or "")
        if not crl_id:
            continue
        group = by_crl.get(crl_id, [])
        if not group:
            continue

        leavers: list[dict[str, Any]] = []
        joiners: list[dict[str, Any]] = []
        stable: list[dict[str, Any]] = []

        for rec in group:
            eid = str(rec.get("employee_external_id") or "").strip()
            if not eid:
                continue
            lwd = parse_sheet_date(rec.get("lwd"), period_start=period_start)
            doj = parse_sheet_date(rec.get("doj"), period_start=period_start)
            in_period_lwd = lwd is not None and period_start <= lwd <= period_end
            in_period_doj = doj is not None and period_start <= doj <= period_end
            if in_period_lwd and not in_period_doj:
                leavers.append(rec)
            elif in_period_doj and not in_period_lwd:
                joiners.append(rec)
            elif in_period_lwd and in_period_doj:
                # Both in month — ambiguous replacement; do not pair.
                stable.append(rec)
            else:
                stable.append(rec)

        if not leavers or not joiners:
            continue
        if len(leavers) != len(joiners):
            log.info(
                "MIS handover skip crl=%s: leaver count %s != joiner count %s",
                crl_id,
                len(leavers),
                len(joiners),
            )
            continue

        paired: list[HandoverPair] = []
        joiners_left = list(joiners)
        ambiguous = False
        for lv in leavers:
            lwd = parse_sheet_date(lv.get("lwd"), period_start=period_start)
            if not lwd:
                ambiguous = True
                break
            best: dict[str, Any] | None = None
            for jn in joiners_left:
                doj = parse_sheet_date(jn.get("doj"), period_start=period_start)
                if not doj or doj <= lwd:
                    continue
                if best is None:
                    best = jn
                else:
                    ambiguous = True
                    break
            if ambiguous or not best:
                ambiguous = True
                break
            joiners_left.remove(best)
            paired.append(
                HandoverPair(
                    crl_id=crl_id,
                    leaver_id=str(lv.get("employee_external_id") or "").strip(),
                    joiner_id=str(best.get("employee_external_id") or "").strip(),
                    leaver_name=str(lv.get("employee_name") or "").strip(),
                    joiner_name=str(best.get("employee_name") or "").strip(),
                )
            )
        if ambiguous or joiners_left:
            log.info("MIS handover skip crl=%s: ambiguous leaver/joiner pairing", crl_id)
            continue

        try:
            cap = int(Decimal(str(rl.get("contracted_quantity") or "0")))
        except Exception:
            cap = 0
        if cap >= 1 and len(stable) + len(paired) > cap:
            log.info(
                "MIS handover skip crl=%s: stable %s + pairs %s > cap %s",
                crl_id,
                len(stable),
                len(paired),
                cap,
            )
            continue

        planned: list[tuple[HandoverPair, dict[str, Any]]] = []
        for p in paired:
            lv_rec = next(r for r in leavers if str(r.get("employee_external_id")).strip() == p.leaver_id)
            jn_rec = next(r for r in joiners if str(r.get("employee_external_id")).strip() == p.joiner_id)
            lwd = parse_sheet_date(lv_rec.get("lwd"), period_start=period_start)
            doj = parse_sheet_date(jn_rec.get("doj"), period_start=period_start)
            if not lwd or not doj or lwd >= doj:
                ambiguous = True
                break
            merged = _merge_daywise_handover(
                lv_rec,
                jn_rec,
                period_start=period_start,
                period_end=period_end,
                leaver_lwd=lwd,
                joiner_doj=doj,
            )
            display = _comma_names(p.joiner_name, p.leaver_name)
            merged["employee_name"] = display
            merged["handover_merged"] = True
            merged["handover_leaver_id"] = p.leaver_id
            planned.append((p, merged))

        if ambiguous or not planned:
            continue

        for p, merged in planned:
            joiner_name_overrides[p.joiner_id] = str(merged.get("employee_name") or "")
            for i, rec in enumerate(working):
                if str(rec.get("employee_external_id") or "").strip() == p.joiner_id:
                    working[i] = merged
                    break
            remove_ids.add(p.leaver_id)
            suppressed.setdefault(crl_id, []).append(p.leaver_id)
            pairs_out.append(
                {
                    "crl_id": crl_id,
                    "leaver_id": p.leaver_id,
                    "joiner_id": p.joiner_id,
                    "joiner_display_name": joiner_name_overrides[p.joiner_id],
                }
            )

    out_records = [r for r in working if str(r.get("employee_external_id") or "").strip() not in remove_ids]

    handover_meta: dict[str, Any] = {
        "applied": bool(pairs_out),
        "pairs": pairs_out,
        "suppressed_employee_ids": sorted(remove_ids),
        "suppressed_by_crl": suppressed,
    }
    if pairs_out:
        log.info(
            "MIS handover pairing site period %s–%s: %s pair(s)",
            period_start,
            period_end,
            len(pairs_out),
        )
    return HandoverPrepResult(records=out_records, handover=handover_meta, suppressed_by_crl=suppressed)


def apply_handover_summary_row_fixup(
    summary_json: dict[str, Any],
    handover: dict[str, Any] | None,
) -> None:
    """After post-LLM cap: omit suppressed leaver rows; sync joiner display name."""
    if not handover or not handover.get("applied"):
        return
    suppressed_by_crl = handover.get("suppressed_by_crl") or {}
    if not isinstance(suppressed_by_crl, dict):
        suppressed_by_crl = {}
    suppressed_global = {
        str(x).strip()
        for x in (handover.get("suppressed_employee_ids") or [])
        if str(x).strip()
    }
    name_by_joiner = {
        p.get("joiner_id"): p.get("joiner_display_name")
        for p in (handover.get("pairs") or [])
        if isinstance(p, dict)
    }
    rows = summary_json.get("summary_rows")
    if not isinstance(rows, list):
        return

    for rr in rows:
        if not isinstance(rr, dict):
            continue
        crl = str(rr.get("contract_rate_line_id") or "").strip()
        eid = str(rr.get("employee_external_id") or "").strip()
        if not eid:
            continue
        suppressed_on_crl = suppressed_by_crl.get(crl) or [] if crl else []
        should_suppress = eid in suppressed_global and (
            not suppressed_on_crl or eid in suppressed_on_crl
        )
        if should_suppress:
            rr["is_omitted"] = True
            rr["final_amount"] = 0.0
            rr["omit_reason"] = (
                "Not billed: LWD/DOJ handover — attendance merged into joiner row for this post."
            )
            note = str(rr.get("calc_notes") or "").strip()
            hdr = "[server] Handover merge: leaver row suppressed; see joiner row on same contract line.\n---\n"
            rr["calc_notes"] = (hdr + note).strip() if note else hdr.strip()
        if eid in name_by_joiner:
            disp = str(name_by_joiner[eid] or "").strip()
            if disp:
                rr["employee_name"] = disp
