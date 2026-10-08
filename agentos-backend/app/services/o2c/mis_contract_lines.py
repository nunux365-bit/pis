"""MIS workflow: copy contract rate lines between terms versions on the same site."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.billing_constants import OHC_INVOICE_ADMIN_ROLE_CODE
from app.agents.o2c_ohc.contract_terms_dates import merged_effective_dates

def _skip_invoice_admin_when_target_has_one_sql(*, src_alias: str) -> str:
    """One invoice-admin line per MIS CTV + site scope (active or inactive)."""
    return f"""
    AND NOT (
      {src_alias}.role_code = :invoice_admin_rc
      AND EXISTS (
        SELECT 1 FROM contract_rate_line tgt_admin
        WHERE tgt_admin.contract_terms_version_id = CAST(:tgt_ctv AS uuid)
          AND tgt_admin.role_code = :invoice_admin_rc
          AND (tgt_admin.service_site_id = CAST(:ssid AS uuid)
               OR tgt_admin.service_site_id IS NULL)
      )
    )
"""


def _crl_commercial_key_match_sql(*, left_alias: str, right_alias: str) -> str:
    """Same commercial identity used to dedupe CTV import and remap override parents."""
    return f"""
        {left_alias}.billing_model IS NOT DISTINCT FROM {right_alias}.billing_model
        AND {left_alias}.role_code IS NOT DISTINCT FROM {right_alias}.role_code
        AND {left_alias}.rate_unit IS NOT DISTINCT FROM {right_alias}.rate_unit
        AND {left_alias}.rate_amount IS NOT DISTINCT FROM {right_alias}.rate_amount
    """


def _crl_not_yet_on_target_ctv_sql(*, src_alias: str) -> str:
    """Dedupe: target MIS CTV already has a line with same site scope + commercial key."""
    return f"""
    AND NOT EXISTS (
      SELECT 1 FROM contract_rate_line existing
      WHERE existing.contract_terms_version_id = CAST(:tgt_ctv AS uuid)
        AND existing.service_site_id IS NOT DISTINCT FROM {src_alias}.service_site_id
        AND {_crl_commercial_key_match_sql(left_alias="existing", right_alias=src_alias)}
    )
"""


def site_override_hides_shared_parent(*, is_active: bool, contracted_quantity: object) -> bool:
    """True when Save must ignore the shared parent for this site.

    Active copy = this site bills the copy. Quantity 0 + inactive = Remove.
    Inactive copy with quantity > 0 (or null) = imported, not yet Added; shared still bills.
    """
    if is_active:
        return True
    if contracted_quantity is None:
        return False
    try:
        return Decimal(str(contracted_quantity)) == 0
    except (ArithmeticError, ValueError, TypeError):
        return False


def skip_global_crl_when_site_override_exists_sql(*, crl_alias: str) -> str:
    """Drop shared (NULL-site) lines when this site has a billing or removed copy.

    Requires bind ``:ssid``. Match is by ``overrides_contract_rate_line_id``, not
    commercial key, so rate edits on the copy cannot un-hide the shared line.

    Inactive imported copies keep quantity > 0 and must not hide the shared parent.
    """
    return f"""
    AND NOT (
      {crl_alias}.service_site_id IS NULL
      AND EXISTS (
        SELECT 1 FROM contract_rate_line site_crl
        WHERE site_crl.overrides_contract_rate_line_id = {crl_alias}.id
          AND site_crl.service_site_id = CAST(:ssid AS uuid)
          AND site_crl.contract_terms_version_id = {crl_alias}.contract_terms_version_id
          AND (
            site_crl.is_active = true
            OR (
              site_crl.is_active = false
              AND site_crl.contracted_quantity = 0
            )
          )
      )
    )
"""


async def list_alternate_contract_terms_for_mis_run(*, mis_run_id: UUID) -> dict:
    """Other pending billable CTVs for the MIS site (excludes the run's current CTV)."""
    mid = str(mis_run_id)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            mr = await session.execute(
                text("""
                SELECT mr.contract_terms_version_id, mr.service_site_id, mr.billing_client_id,
                       mr.billing_period_start, mr.billing_period_end, mr.status
                FROM o2c_mis_run mr
                WHERE mr.id = CAST(:mid AS uuid)
                """),
                {"mid": mid},
            )
            row = mr.mappings().first()
            if not row:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "MIS run not found")
            d = dict(row)
            if str(d.get("status") or "") != "pending_human":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Rate lines can only be imported while MIS is pending_human",
                )
            tgt_ctv = str(d["contract_terms_version_id"])
            ssid = str(d["service_site_id"])
            bind = {
                "mid": mid,
                "tgt_ctv": tgt_ctv,
                "ssid": ssid,
                "bc": str(d["billing_client_id"]),
                "ps": d["billing_period_start"],
                "pe": d["billing_period_end"],
                "invoice_admin_rc": OHC_INVOICE_ADMIN_ROLE_CODE,
            }
            r = await session.execute(
                text(
                    f"""
                SELECT ctv.id::text AS contract_terms_version_id,
                       ctv.status,
                       ctv.title,
                       ctv.effective_from,
                       ctv.effective_to,
                       ctv.created_at,
                       count(crl.id)::int AS line_count,
                       (
                         SELECT count(*)::int
                         FROM contract_rate_line src_crl
                         WHERE src_crl.contract_terms_version_id = ctv.id
                           AND src_crl.is_active = true
                           AND (src_crl.service_site_id = CAST(:ssid AS uuid)
                                OR src_crl.service_site_id IS NULL)
                           {_crl_not_yet_on_target_ctv_sql(src_alias="src_crl")}
                           {_skip_invoice_admin_when_target_has_one_sql(src_alias="src_crl")}
                       ) AS importable_line_count
                FROM contract_terms_version ctv
                JOIN contract_rate_line crl ON crl.contract_terms_version_id = ctv.id
                    AND crl.is_active = true
                    AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
                WHERE ctv.billing_client_id = CAST(:bc AS uuid)
                  AND ctv.id <> CAST(:tgt_ctv AS uuid)
                  AND ctv.status = 'pending'
                  AND ctv.superseded_by_id IS NULL
                  AND ctv.effective_from <= CAST(:pe AS date)
                  AND (ctv.effective_to IS NULL OR ctv.effective_to >= CAST(:ps AS date))
                GROUP BY ctv.id, ctv.status, ctv.title, ctv.effective_from, ctv.effective_to, ctv.created_at
                ORDER BY ctv.created_at DESC
                """
                ),
                bind,
            )
            items = [dict(x) for x in r.mappings().all()]
    return {"items": items}


async def copy_contract_rate_lines_to_mis_run(
    *,
    mis_run_id: UUID,
    source_contract_terms_version_id: UUID,
) -> dict:
    """Copy active site/global lines from source CTV onto the MIS run's CTV; extend target dates."""
    mid = str(mis_run_id)
    src_ctv = str(source_contract_terms_version_id)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            mr = await session.execute(
                text("""
                SELECT mr.contract_terms_version_id, mr.service_site_id, mr.billing_client_id,
                       mr.billing_period_start, mr.billing_period_end, mr.status
                FROM o2c_mis_run mr
                WHERE mr.id = CAST(:mid AS uuid)
                FOR UPDATE
                """),
                {"mid": mid},
            )
            mr_row = mr.mappings().first()
            if not mr_row:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "MIS run not found")
            run = dict(mr_row)
            if str(run.get("status") or "") != "pending_human":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Import only allowed while MIS is pending_human",
                )
            tgt_ctv = str(run["contract_terms_version_id"])
            ssid = str(run["service_site_id"])
            if src_ctv == tgt_ctv:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Source and target contract are the same")

            chk = await session.execute(
                text("""
                SELECT ctv.id
                FROM contract_terms_version ctv
                WHERE ctv.id = CAST(:src AS uuid)
                  AND ctv.billing_client_id = CAST(:bc AS uuid)
                  AND ctv.status = 'pending'
                  AND ctv.superseded_by_id IS NULL
                  AND ctv.effective_from <= CAST(:pe AS date)
                  AND (ctv.effective_to IS NULL OR ctv.effective_to >= CAST(:ps AS date))
                  AND EXISTS (
                    SELECT 1 FROM contract_rate_line crl
                    WHERE crl.contract_terms_version_id = ctv.id
                      AND crl.is_active = true
                      AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
                  )
                """),
                {
                    "src": src_ctv,
                    "bc": str(run["billing_client_id"]),
                    "ps": run["billing_period_start"],
                    "pe": run["billing_period_end"],
                    "ssid": ssid,
                },
            )
            if not chk.mappings().first():
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "Source pending contract not found for this client/site/period",
                )

            dedupe = _crl_not_yet_on_target_ctv_sql(src_alias="crl")
            skip_admin = _skip_invoice_admin_when_target_has_one_sql(src_alias="crl")
            parent_key = _crl_commercial_key_match_sql(left_alias="tp", right_alias="sp")
            ins = await session.execute(
                text(
                    f"""
                INSERT INTO contract_rate_line (
                    id, service_site_id, contract_terms_version_id,
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
                    crl.service_site_id,
                    CAST(:tgt_ctv AS uuid),
                    crl.billing_model, crl.role_code, crl.description,
                    crl.rate_amount, crl.rate_unit, crl.contracted_quantity,
                    crl.attendance_required, crl.minimum_units_per_period,
                    crl.unfilled_penalty_pct, crl.ot_multiplier,
                    crl.service_charge_type, crl.service_charge_value, crl.actuals_markup_pct,
                    crl.schedule_type, crl.schedule_config,
                    crl.billing_rules, crl.billing_rule_text, crl.model_config, crl.source_ref,
                    crl.currency, crl.effective_from, crl.effective_to, false,
                    mapped.id
                FROM contract_rate_line crl
                LEFT JOIN LATERAL (
                    SELECT tp.id
                    FROM contract_rate_line sp
                    JOIN contract_rate_line tp
                      ON tp.contract_terms_version_id = CAST(:tgt_ctv AS uuid)
                     AND tp.service_site_id IS NULL
                     AND {parent_key}
                    WHERE sp.id = crl.overrides_contract_rate_line_id
                      AND sp.service_site_id IS NULL
                      AND (
                        SELECT count(*)::int
                        FROM contract_rate_line tp2
                        WHERE tp2.contract_terms_version_id = CAST(:tgt_ctv AS uuid)
                          AND tp2.service_site_id IS NULL
                          AND {_crl_commercial_key_match_sql(left_alias="tp2", right_alias="sp")}
                      ) = 1
                    ORDER BY tp.id
                    LIMIT 1
                ) mapped ON crl.overrides_contract_rate_line_id IS NOT NULL
                WHERE crl.contract_terms_version_id = CAST(:src_ctv AS uuid)
                  AND crl.is_active = true
                  AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
                  AND (crl.overrides_contract_rate_line_id IS NULL OR mapped.id IS NOT NULL)
                  AND NOT EXISTS (
                    SELECT 1 FROM contract_rate_line existing_ov
                    WHERE mapped.id IS NOT NULL
                      AND existing_ov.service_site_id = CAST(:ssid AS uuid)
                      AND existing_ov.overrides_contract_rate_line_id = mapped.id
                  )
                  {dedupe}
                  {skip_admin}
                """
                ),
                {
                    "tgt_ctv": tgt_ctv,
                    "src_ctv": src_ctv,
                    "ssid": ssid,
                    "invoice_admin_rc": OHC_INVOICE_ADMIN_ROLE_CODE,
                },
            )
            cloned = ins.rowcount or 0
            if cloned <= 0:
                return {
                    "status": "ok",
                    "cloned_count": 0,
                    "target_contract_terms_version_id": tgt_ctv,
                    "source_contract_terms_version_id": src_ctv,
                }

            ctv_r = await session.execute(
                text("""
                SELECT effective_from, effective_to FROM contract_terms_version
                WHERE id = CAST(:id AS uuid)
                """),
                {"id": tgt_ctv},
            )
            ctv = dict(ctv_r.mappings().first() or {})
            lines_r = await session.execute(
                text("""
                SELECT effective_from, effective_to FROM contract_rate_line
                WHERE contract_terms_version_id = CAST(:ctv AS uuid)
                  AND (service_site_id = CAST(:ssid AS uuid) OR service_site_id IS NULL)
                """),
                {"ctv": tgt_ctv, "ssid": ssid},
            )
            extra_froms: list[date] = []
            extra_tos: list[date] = []
            for ln in lines_r.mappings().all():
                ld = dict(ln)
                if ld.get("effective_from"):
                    extra_froms.append(ld["effective_from"])
                if ld.get("effective_to"):
                    extra_tos.append(ld["effective_to"])
            new_from, new_to = merged_effective_dates(
                current_from=ctv.get("effective_from"),
                current_to=ctv.get("effective_to"),
                period_start=run["billing_period_start"],
                period_end=run["billing_period_end"],
                extra_froms=extra_froms,
                extra_to_caps=extra_tos,
            )
            await session.execute(
                text("""
                UPDATE contract_terms_version
                SET effective_from = CAST(:ef AS date),
                    effective_to = CAST(:et AS date),
                    updated_at = now()
                WHERE id = CAST(:id AS uuid)
                """),
                {"id": tgt_ctv, "ef": new_from, "et": new_to},
            )

    return {
        "status": "ok",
        "cloned_count": cloned,
        "target_contract_terms_version_id": tgt_ctv,
        "source_contract_terms_version_id": src_ctv,
    }


async def approve_contract_terms_for_mis_run(
    session,
    *,
    contract_terms_version_id: str,
    service_site_id: str,
    period_start: date,
    period_end: date,
) -> None:
    """Set CTV approved and widen effective dates (same transaction as MIS approve)."""
    ctv_r = await session.execute(
        text("""
        SELECT effective_from, effective_to FROM contract_terms_version
        WHERE id = CAST(:id AS uuid)
        """),
        {"id": contract_terms_version_id},
    )
    ctv = dict(ctv_r.mappings().first() or {})
    lines_r = await session.execute(
        text("""
        SELECT effective_from, effective_to FROM contract_rate_line
        WHERE contract_terms_version_id = CAST(:ctv AS uuid)
          AND is_active = true
          AND (service_site_id = CAST(:ssid AS uuid) OR service_site_id IS NULL)
        """),
        {"ctv": contract_terms_version_id, "ssid": service_site_id},
    )
    extra_froms: list[date] = []
    extra_tos: list[date] = []
    for ln in lines_r.mappings().all():
        ld = dict(ln)
        if ld.get("effective_from"):
            extra_froms.append(ld["effective_from"])
        if ld.get("effective_to"):
            extra_tos.append(ld["effective_to"])
    new_from, new_to = merged_effective_dates(
        current_from=ctv.get("effective_from"),
        current_to=ctv.get("effective_to"),
        period_start=period_start,
        period_end=period_end,
        extra_froms=extra_froms,
        extra_to_caps=extra_tos,
    )
    await session.execute(
        text("""
        UPDATE contract_terms_version
        SET status = 'approved',
            effective_from = CAST(:ef AS date),
            effective_to = CAST(:et AS date),
            updated_at = now()
        WHERE id = CAST(:id AS uuid)
          AND status IN ('pending', 'approved')
          AND superseded_by_id IS NULL
        """),
        {
            "id": contract_terms_version_id,
            "ef": new_from,
            "et": new_to,
        },
    )
