"""MIS auto-approve after draft/rerun: compare to prior approved month, then approve if eligible."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.attendance_site_recon import _is_undefined_table
from app.agents.o2c_ohc.mis_drafts import (
    _mis_duplicate_employee_staffing_anomaly_message,
    _rate_lines_for_site_async,
)
from app.agents.o2c_ohc.mis_summary_llm import (
    MIS_SERVICE_CHARGE_EXTERNAL_ID,
    NON_EMPLOYEE_SENTINEL,
)
from app.config.settings import settings
from app.services.o2c.mis_contract_lines import skip_global_crl_when_site_override_exists_sql

log = logging.getLogger(__name__)

AUTO_APPROVE_ACTOR = "system:auto_approve"
AUTO_APPROVE_NOTE_PREFIX = "[auto_approve] "


def parse_latest_auto_approve_audit(notes: str | None) -> dict[str, Any] | None:
    """Parse the most recent ``[auto_approve] {json}`` line from ``o2c_mis_run.notes``."""
    if not notes:
        return None
    last: dict[str, Any] | None = None
    for line in str(notes).splitlines():
        stripped = line.strip()
        if not stripped.startswith(AUTO_APPROVE_NOTE_PREFIX):
            continue
        raw = stripped[len(AUTO_APPROVE_NOTE_PREFIX) :].strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                last = parsed
        except json.JSONDecodeError:
            continue
    return last


@dataclass
class MisBillSnapshot:
    mis_run_id: str
    total_amount: Decimal
    employee_count_by_role: dict[str, int]
    line_item_count: int
    line_item_total: Decimal


@dataclass
class AutoApproveDecision:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    prior_mis_run_id: str | None = None
    current_total: Decimal | None = None
    prior_total: Decimal | None = None
    pct_delta: float | None = None
    current_line_item_total: Decimal | None = None
    prior_line_item_total: Decimal | None = None
    line_item_pct_delta: float | None = None


def _auto_approve_enabled() -> bool:
    return bool(getattr(settings, "o2c_mis_auto_approve_enabled", True))


def _tolerance_pct() -> float:
    return float(getattr(settings, "o2c_mis_auto_approve_tolerance_pct", 5.0))


def _site_blocklist() -> set[str]:
    raw = (getattr(settings, "o2c_mis_auto_approve_site_blocklist", None) or "").strip()
    if not raw:
        return set()
    return {s.strip() for s in raw.split(",") if s.strip()}


def prior_month_start(period_start: date) -> date:
    first = period_start.replace(day=1)
    prev_end = first - timedelta(days=1)
    return prev_end.replace(day=1)


def _is_employee_row(employee_external_id: str | None) -> bool:
    s = str(employee_external_id or "").strip()
    return bool(s) and s not in (NON_EMPLOYEE_SENTINEL, MIS_SERVICE_CHARGE_EXTERNAL_ID)


def _pct_delta_abs(current: Decimal, prior: Decimal) -> float | None:
    if prior <= 0:
        return None
    return float(abs(current - prior) / prior * Decimal(100))


def _rate_amount_fingerprint_part(raw: Any) -> str:
    """Canonical rate for fingerprint (``6000`` vs ``6000.0000`` → same).

    On parse/normalize failure, returns trimmed raw text — never raises.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        d = Decimal(s)
        if not d.is_finite():
            return s
        d = d.normalize()
        txt = format(d, "f")
        if "." in txt:
            txt = txt.rstrip("0").rstrip(".")
        return txt or "0"
    except (InvalidOperation, ArithmeticError, ValueError, TypeError):
        return s


def _coerce_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _summary_json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def _fetch_run_header(session: Any, mis_run_id: str) -> dict[str, Any] | None:
    r = await session.execute(
        text("""
        SELECT id::text AS id, status, client_site_key, service_site_id::text AS service_site_id,
               contract_terms_version_id::text AS contract_terms_version_id,
               billing_period_start, billing_period_end, summary_json
        FROM o2c_mis_run
        WHERE id = CAST(:mid AS uuid)
        """),
        {"mid": mis_run_id},
    )
    row = r.mappings().first()
    return dict(row) if row else None


async def _build_bill_snapshot(session: Any, mis_run_id: str) -> MisBillSnapshot:
    r = await session.execute(
        text("""
        SELECT coalesce(nullif(trim(role_code), ''), '(none)') AS role_code,
               employee_external_id,
               final_amount
        FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid) AND NOT is_omitted
        """),
        {"mid": mis_run_id},
    )
    by_role: dict[str, int] = {}
    line_items = 0
    line_item_total = Decimal(0)
    total = Decimal(0)
    for row in r.mappings().all():
        amt = Decimal(str(row["final_amount"] or 0))
        total += amt
        if _is_employee_row(row.get("employee_external_id")):
            rc = str(row["role_code"] or "(none)")
            by_role[rc] = by_role.get(rc, 0) + 1
        else:
            line_items += 1
            line_item_total += amt
    return MisBillSnapshot(
        mis_run_id=mis_run_id,
        total_amount=total,
        employee_count_by_role=by_role,
        line_item_count=line_items,
        line_item_total=line_item_total,
    )


async def _contract_fingerprint(
    session: Any, *, contract_terms_version_id: str, service_site_id: str
) -> tuple[tuple[str, str, str, str], ...]:
    skip_global = skip_global_crl_when_site_override_exists_sql(crl_alias="contract_rate_line")
    r = await session.execute(
        text(f"""
        SELECT role_code, billing_model::text AS billing_model,
               coalesce(contracted_quantity::text, '') AS contracted_quantity,
               coalesce(rate_amount::text, '') AS rate_amount
        FROM contract_rate_line
        WHERE contract_terms_version_id = CAST(:ctv AS uuid)
          AND is_active = true
          AND (service_site_id = CAST(:ssid AS uuid) OR service_site_id IS NULL)
          {skip_global}
        ORDER BY role_code, billing_model, contracted_quantity, rate_amount, id
        """),
        {"ctv": contract_terms_version_id, "ssid": service_site_id},
    )
    return tuple(
        (
            str(row["role_code"] or ""),
            str(row["billing_model"] or ""),
            str(row["contracted_quantity"] or ""),
            _rate_amount_fingerprint_part(row.get("rate_amount")),
        )
        for row in r.mappings().all()
    )


async def _has_open_attendance_recon(
    session: Any, *, client_site_key: str, period_start: date, period_end: date
) -> bool:
    try:
        r = await session.execute(
            text("""
            SELECT 1 FROM o2c_attendance_site_recon
            WHERE client_site_key = :csk
              AND billing_period_start = CAST(:ps AS date)
              AND billing_period_end = CAST(:pe AS date)
              AND status = 'open'
            LIMIT 1
            """),
            {"csk": client_site_key, "ps": period_start, "pe": period_end},
        )
        return r.first() is not None
    except ProgrammingError as e:
        await session.rollback()
        if _is_undefined_table(e):
            log.debug("o2c_attendance_site_recon missing; treat as no open recon")
            return False
        log.warning("open attendance recon check failed: %s", e)
        return False
    except Exception as e:
        await session.rollback()
        log.warning("open attendance recon check failed: %s", e)
        return False


async def _has_human_correction(session: Any, mis_run_id: str) -> bool:
    r = await session.execute(
        text("""
        SELECT 1 FROM o2c_mis_summary_row
        WHERE mis_run_id = CAST(:mid AS uuid) AND human_correction IS NOT NULL
        LIMIT 1
        """),
        {"mid": mis_run_id},
    )
    return r.first() is not None


async def evaluate_auto_approve_eligibility(mis_run_id: UUID) -> AutoApproveDecision:
    """Return whether MIS may be auto-approved vs prior month approved run."""
    mid = str(mis_run_id)
    reasons: list[str] = []

    if not _auto_approve_enabled():
        return AutoApproveDecision(eligible=False, reasons=["auto_approve_disabled"])

    prior_id: str | None = None
    current_snap: MisBillSnapshot | None = None
    prior_snap: MisBillSnapshot | None = None
    pct: float | None = None
    line_item_pct: float | None = None
    summary_json: dict[str, Any] = {}
    ctv_id = ""
    ss_id = ""

    async with AgenosAsyncSessionLocal() as session:
        hdr = await _fetch_run_header(session, mid)
        if not hdr:
            return AutoApproveDecision(eligible=False, reasons=["mis_run_not_found"])

        status = str(hdr.get("status") or "")
        if status != "pending_human":
            return AutoApproveDecision(eligible=False, reasons=[f"status_{status}"])

        csk = str(hdr.get("client_site_key") or "").strip()
        if csk in _site_blocklist():
            return AutoApproveDecision(eligible=False, reasons=["site_blocklisted"])

        ss_id = str(hdr.get("service_site_id") or "")
        ctv_id = str(hdr.get("contract_terms_version_id") or "")
        period_start = _coerce_date(hdr["billing_period_start"])
        period_end = _coerce_date(hdr["billing_period_end"])
        summary_json = _summary_json_dict(hdr.get("summary_json"))

        handover = summary_json.get("handover")
        if isinstance(handover, dict) and handover.get("applied"):
            reasons.append("lwd_doj_handover_applied")

        if await _has_open_attendance_recon(
            session, client_site_key=csk, period_start=period_start, period_end=period_end
        ):
            reasons.append("open_attendance_recon")

        prior_ps = prior_month_start(period_start)
        pr = await session.execute(
            text("""
            SELECT id::text AS id, contract_terms_version_id::text AS contract_terms_version_id
            FROM o2c_mis_run
            WHERE service_site_id = CAST(:ssid AS uuid)
              AND billing_period_start = CAST(:ps AS date)
              AND status = 'approved'
            ORDER BY approved_at DESC NULLS LAST
            LIMIT 1
            """),
            {"ssid": ss_id, "ps": prior_ps},
        )
        prior_row = pr.mappings().first()
        if not prior_row:
            reasons.append("no_prior_approved_mis")
        else:
            prior_id = str(prior_row["id"])
            current_snap = await _build_bill_snapshot(session, mid)
            prior_snap = await _build_bill_snapshot(session, prior_id)

            if prior_snap.total_amount <= 0:
                reasons.append("prior_total_zero")

            if current_snap.employee_count_by_role != prior_snap.employee_count_by_role:
                reasons.append("employee_count_by_role_mismatch")

            if current_snap.line_item_count != prior_snap.line_item_count:
                reasons.append("line_item_count_mismatch")

            cur_fp = await _contract_fingerprint(
                session, contract_terms_version_id=ctv_id, service_site_id=ss_id
            )
            prior_ctv = str(prior_row["contract_terms_version_id"] or "")
            prior_fp = await _contract_fingerprint(
                session, contract_terms_version_id=prior_ctv, service_site_id=ss_id
            )
            if cur_fp != prior_fp:
                reasons.append("contract_rate_line_fingerprint_mismatch")

            if current_snap.total_amount <= 0:
                reasons.append("current_total_zero")

            if prior_snap.total_amount > 0:
                pct = _pct_delta_abs(current_snap.total_amount, prior_snap.total_amount)
                if pct is not None and pct > _tolerance_pct():
                    reasons.append(f"amount_delta_{pct:.2f}pct_gt_{_tolerance_pct():g}")

            if prior_snap.line_item_total <= 0 and current_snap.line_item_total > 0:
                reasons.append("line_item_total_prior_zero")
            elif prior_snap.line_item_total > 0:
                line_item_pct = _pct_delta_abs(
                    current_snap.line_item_total, prior_snap.line_item_total
                )
                if line_item_pct is not None and line_item_pct > _tolerance_pct():
                    reasons.append(
                        f"line_item_total_delta_{line_item_pct:.2f}pct_gt_{_tolerance_pct():g}"
                    )

        if await _has_human_correction(session, mid):
            reasons.append("human_correction_present")

        rate_lines = await _rate_lines_for_site_async(session, ctv_id, ss_id)
        if _mis_duplicate_employee_staffing_anomaly_message(summary_json, rate_lines):
            reasons.append("duplicate_staffing_billing")

    eligible = len(reasons) == 0
    return AutoApproveDecision(
        eligible=eligible,
        reasons=reasons,
        prior_mis_run_id=prior_id,
        current_total=current_snap.total_amount if current_snap else None,
        prior_total=prior_snap.total_amount if prior_snap else None,
        pct_delta=pct,
        current_line_item_total=current_snap.line_item_total if current_snap else None,
        prior_line_item_total=prior_snap.line_item_total if prior_snap else None,
        line_item_pct_delta=line_item_pct,
    )


async def _persist_auto_approve_audit(
    mis_run_id: UUID,
    *,
    decision: AutoApproveDecision,
    approved: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "approved": approved,
        "eligible": decision.eligible,
        "reasons": decision.reasons,
        "prior_mis_run_id": decision.prior_mis_run_id,
        "current_total": str(decision.current_total) if decision.current_total is not None else None,
        "prior_total": str(decision.prior_total) if decision.prior_total is not None else None,
        "pct_delta": decision.pct_delta,
        "current_line_item_total": (
            str(decision.current_line_item_total)
            if decision.current_line_item_total is not None
            else None
        ),
        "prior_line_item_total": (
            str(decision.prior_line_item_total)
            if decision.prior_line_item_total is not None
            else None
        ),
        "line_item_pct_delta": decision.line_item_pct_delta,
        "tolerance_pct": _tolerance_pct(),
    }
    if extra:
        payload.update(extra)
    note_line = "[auto_approve] " + json.dumps(payload, default=str)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("""
                UPDATE o2c_mis_run
                SET notes = trim(both from coalesce(notes, '') || E'\\n' || :note),
                    updated_at = now()
                WHERE id = CAST(:mid AS uuid)
                """),
                {"mid": str(mis_run_id), "note": note_line[:8000]},
            )


async def attach_auto_approve_after_draft(
    resp: dict[str, Any], *, mis_run_id: str | None, draft_status: str
) -> dict[str, Any]:
    """Run auto-approve after successful draft; never fail the caller response."""
    if draft_status != "ok" or not mis_run_id:
        resp["auto_approve"] = {"attempted": False, "approved": False, "reason": "draft_not_ok"}
        return resp
    try:
        resp["auto_approve"] = await try_auto_approve_mis_run(UUID(str(mis_run_id)))
    except Exception as e:
        log.exception("MIS auto-approve unexpected error run=%s", mis_run_id)
        resp["auto_approve"] = {"attempted": True, "approved": False, "error": str(e)}
    return resp


async def try_auto_approve_mis_run(mis_run_id: UUID) -> dict[str, Any]:
    """
    Evaluate and approve MIS when eligible. Never raises — returns outcome dict for API/logging.
    """
    if not _auto_approve_enabled():
        return {"attempted": False, "approved": False, "reason": "disabled"}

    try:
        decision = await evaluate_auto_approve_eligibility(mis_run_id)
    except Exception as e:
        log.exception("MIS auto-approve eligibility failed run=%s", mis_run_id)
        return {"attempted": True, "approved": False, "error": str(e)}

    if not decision.eligible:
        try:
            await _persist_auto_approve_audit(mis_run_id, decision=decision, approved=False)
        except Exception:
            log.exception("MIS auto-approve audit persist failed run=%s", mis_run_id)
        return {
            "attempted": True,
            "approved": False,
            "reasons": decision.reasons,
            "pct_delta": decision.pct_delta,
            "prior_mis_run_id": decision.prior_mis_run_id,
        }

    try:
        from app.services.o2c.mis_workflow import approve_mis_with_xlsx_export

        result = await approve_mis_with_xlsx_export(
            mis_run_id=mis_run_id,
            approved_by=AUTO_APPROVE_ACTOR,
        )
        await _persist_auto_approve_audit(
            mis_run_id,
            decision=decision,
            approved=True,
            extra={"xlsx_error": result.get("xlsx_error")},
        )
        return {
            "attempted": True,
            "approved": True,
            "reasons": [],
            "pct_delta": decision.pct_delta,
            "prior_mis_run_id": decision.prior_mis_run_id,
            "mis_status": result.get("mis_status"),
            "xlsx_error": result.get("xlsx_error"),
        }
    except Exception as e:
        log.exception("MIS auto-approve failed run=%s", mis_run_id)
        try:
            await _persist_auto_approve_audit(
                mis_run_id,
                decision=decision,
                approved=False,
                extra={"approve_error": str(e)},
            )
        except Exception:
            log.exception("MIS auto-approve audit persist failed run=%s", mis_run_id)
        return {
            "attempted": True,
            "approved": False,
            "reasons": decision.reasons,
            "error": str(e),
            "pct_delta": decision.pct_delta,
        }
