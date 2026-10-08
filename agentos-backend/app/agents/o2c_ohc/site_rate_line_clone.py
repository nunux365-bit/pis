"""Clone site-scoped contract_rate_line rows from one service_site to another (same billing client)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.mis_drafts import _pick_terms_version_async


async def _count_site_scoped_lines(session: Any, service_site_id: str) -> int:
    r = await session.execute(
        text("""
        SELECT count(*)::int AS c
        FROM contract_rate_line
        WHERE service_site_id = CAST(:sid AS uuid)
        """),
        {"sid": service_site_id},
    )
    row = r.mappings().first()
    return int(row["c"] if row else 0)


async def _delete_unreferenced_site_lines(session: Any, service_site_id: str) -> int:
    r = await session.execute(
        text("""
        DELETE FROM contract_rate_line crl
        WHERE crl.service_site_id = CAST(:sid AS uuid)
          AND NOT EXISTS (
              SELECT 1 FROM o2c_mis_summary_row s WHERE s.contract_rate_line_id = crl.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM invoice_line il WHERE il.contract_rate_line_id = crl.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM package_usage_period p WHERE p.contract_rate_line_id = crl.id
          )
        """),
        {"sid": service_site_id},
    )
    return r.rowcount or 0


async def clone_site_scoped_rate_lines(
    *,
    source_service_site_id: str,
    target_service_site_id: str,
    replace_existing: bool = False,
    require_same_terms_for_period: bool = False,
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any]:
    """
    Copy rows where service_site_id = source onto target (new UUIDs).

    Does not copy contract-wide rows (service_site_id IS NULL).

    When replace_existing is True, deletes existing site-scoped lines on the target that are not
    referenced by MIS / invoices / package usage. If any site-scoped lines remain, raises 409.

    When require_same_terms_for_period is True, both sites must resolve to the same
    contract_terms_version_id for the given period (via _pick_terms_version_async).
    """
    src = (source_service_site_id or "").strip()
    tgt = (target_service_site_id or "").strip()
    if not src or not tgt:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "source and target service_site_id required")
    if src == tgt:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "source and target must differ")

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT id::text AS id, billing_client_id::text AS billing_client_id
                FROM service_site
                WHERE id IN (CAST(:src AS uuid), CAST(:tgt AS uuid))
                """),
                {"src": src, "tgt": tgt},
            )
            site_rows = {str(row["id"]): dict(row) for row in r.mappings().all()}
            if src not in site_rows or tgt not in site_rows:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "source or target service_site not found")
            bc_src = site_rows[src]["billing_client_id"]
            bc_tgt = site_rows[tgt]["billing_client_id"]
            if bc_src != bc_tgt:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Source and target sites must belong to the same billing client",
                )

            if require_same_terms_for_period:
                if period_start is None or period_end is None:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "period_start and period_end are required when require_same_terms_for_period is true",
                    )
                tv_s = await _pick_terms_version_async(
                    session, bc_src, src, period_start=period_start, period_end=period_end
                )
                tv_t = await _pick_terms_version_async(
                    session, bc_tgt, tgt, period_start=period_start, period_end=period_end
                )
                id_s = str(tv_s["contract_terms_version_id"]) if tv_s else ""
                id_t = str(tv_t["contract_terms_version_id"]) if tv_t else ""
                if not id_s or not id_t or id_s != id_t:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "Sites do not share the same billable contract_terms_version for the given period",
                    )

            existing = await _count_site_scoped_lines(session, tgt)
            deleted = 0
            if existing > 0:
                if not replace_existing:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "Target site already has site-scoped rate lines. "
                        "Pass replace_existing=true to remove unreferenced lines first, or clone before adding lines.",
                    )
                deleted = await _delete_unreferenced_site_lines(session, tgt)
                remaining = await _count_site_scoped_lines(session, tgt)
                if remaining > 0:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        f"{remaining} site-scoped rate line(s) on the target are still referenced by MIS or "
                        "invoices and cannot be removed automatically.",
                    )

            ins = await session.execute(
                text("""
                INSERT INTO contract_rate_line (
                    id, service_site_id,
                    contract_terms_version_id,
                    billing_model, role_code, description,
                    rate_amount, rate_unit, contracted_quantity,
                    attendance_required, minimum_units_per_period,
                    unfilled_penalty_pct, ot_multiplier,
                    service_charge_type, service_charge_value, actuals_markup_pct,
                    schedule_type, schedule_config,
                    billing_rules, billing_rule_text, model_config, source_ref,
                    currency, effective_from, effective_to, is_active,
                    overrides_contract_rate_line_id
                )
                SELECT
                    gen_random_uuid(),
                    CAST(:tgt AS uuid),
                    src.contract_terms_version_id,
                    src.billing_model, src.role_code, src.description,
                    src.rate_amount, src.rate_unit, src.contracted_quantity,
                    src.attendance_required, src.minimum_units_per_period,
                    src.unfilled_penalty_pct, src.ot_multiplier,
                    src.service_charge_type, src.service_charge_value, src.actuals_markup_pct,
                    src.schedule_type, src.schedule_config,
                    src.billing_rules, src.billing_rule_text, src.model_config, src.source_ref,
                    src.currency, src.effective_from, src.effective_to, src.is_active,
                    src.overrides_contract_rate_line_id
                FROM contract_rate_line src
                WHERE src.service_site_id = CAST(:src AS uuid)
                  AND NOT (
                    src.overrides_contract_rate_line_id IS NOT NULL
                    AND src.is_active = false
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM contract_rate_line tgt
                    WHERE tgt.service_site_id = CAST(:tgt AS uuid)
                      AND src.overrides_contract_rate_line_id IS NOT NULL
                      AND tgt.overrides_contract_rate_line_id = src.overrides_contract_rate_line_id
                  )
                ORDER BY src.billing_model, src.role_code, src.id
                """),
                {"tgt": tgt, "src": src},
            )
            cloned = ins.rowcount or 0

            msg = None
            if cloned == 0:
                msg = (
                    "No site-scoped rate lines on the source site — only contract-wide lines (if any) apply "
                    "via the shared contract."
                )
            return {
                "status": "ok",
                "cloned_count": cloned,
                "deleted_existing_count": deleted,
                "source_service_site_id": src,
                "target_service_site_id": tgt,
                "message": msg,
            }
