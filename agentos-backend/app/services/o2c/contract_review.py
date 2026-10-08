"""Async agenos queries for contract review UI (O2C)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.ingest_helpers import _parse_iso_date
from app.agents.o2c_ohc.terms_picker import list_billable_terms_for_site_async
from app.schemas.o2c import ContractRateLinePatchItem, validate_contract_rate_line_patch
from app.services.o2c.contract_review_health import _parse_period


def would_expire_remove_last_billable_approved(
    approved_billable_ctv_ids: list[str],
    ctv_id_to_expire: str,
) -> bool:
    """True if expiring this CTV would leave the site with no approved billable contract."""
    if not approved_billable_ctv_ids:
        return False
    remaining = [x for x in approved_billable_ctv_ids if x != ctv_id_to_expire]
    return len(remaining) == 0


async def _validate_expire_safe_for_period(
    session,
    *,
    contract_terms_version_id: str,
    period_start,
    period_end,
) -> None:
    ctv_id = str(contract_terms_version_id)
    sites_r = await session.execute(
        text("""
        SELECT DISTINCT crl.service_site_id::text AS service_site_id
        FROM contract_rate_line crl
        WHERE crl.contract_terms_version_id = CAST(:ctv AS uuid)
          AND crl.is_active = true
          AND crl.service_site_id IS NOT NULL
        """),
        {"ctv": ctv_id},
    )
    site_ids = [str(row["service_site_id"]) for row in sites_r.mappings().all() if row.get("service_site_id")]
    if not site_ids:
        return

    bc_r = await session.execute(
        text("SELECT billing_client_id::text AS bc FROM contract_terms_version WHERE id = CAST(:id AS uuid)"),
        {"id": ctv_id},
    )
    bc_row = bc_r.mappings().first()
    if not bc_row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contract_terms_version not found")
    bc = str(bc_row["bc"])

    for sid in site_ids:
        rows = await list_billable_terms_for_site_async(
            session,
            bc,
            sid,
            period_start=period_start,
            period_end=period_end,
        )
        approved_ids = [
            str(r["contract_terms_version_id"])
            for r in rows
            if str(r.get("status") or "").lower() == "approved"
        ]
        if not would_expire_remove_last_billable_approved(approved_ids, ctv_id):
            continue
        name_r = await session.execute(
            text("""
            SELECT COALESCE(display_name, canonical_name, site_key, id::text) AS site_name
            FROM service_site WHERE id = CAST(:sid AS uuid)
            """),
            {"sid": sid},
        )
        site_name = str((name_r.mappings().first() or {}).get("site_name") or sid)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot expire: this is the only approved contract billable for "
                f"{site_name} in {period_start.isoformat()}–{period_end.isoformat()}. "
                "MIS would have no contract to pick. Expire a duplicate instead, or fix dates."
            ),
        )


def parse_contract_review_statuses_query(statuses: str) -> list[str]:
    if (statuses or "").strip() == "__all__":
        return []
    return [x.strip() for x in statuses.split(",") if x.strip()]


async def contract_review_list(
    *,
    statuses: list[str],
    search: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    p = max(1, int(page))
    ps = max(1, min(int(page_size), 100))
    offset = (p - 1) * ps
    sts = [s.strip().lower() for s in (statuses or []) if str(s).strip()]
    q = (search or "").strip().lower()
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r1 = await session.execute(
                text("""
                SELECT count(*)::int AS total_count
                FROM contract_terms_version ctv
                JOIN billing_client bc ON bc.id = ctv.billing_client_id
                WHERE ((cardinality(CAST(:sts AS text[])) = 0) OR (ctv.status::text = ANY(CAST(:sts AS text[]))))
                  AND (
                    :q = ''
                    OR lower(COALESCE(ctv.title, '')) LIKE '%%' || :q || '%%'
                    OR lower(COALESCE(ctv.ref_number, '')) LIKE '%%' || :q || '%%'
                    OR lower(COALESCE(bc.name, '')) LIKE '%%' || :q || '%%'
                  )
                """),
                {"sts": sts, "q": q},
            )
            total = int((r1.mappings().first() or {}).get("total_count") or 0)
            r2 = await session.execute(
                text("""
                SELECT
                    ctv.id,
                    ctv.title,
                    ctv.contract_kind,
                    ctv.status,
                    ctv.effective_from,
                    ctv.effective_to,
                    ctv.created_at,
                    ctv.updated_at,
                    bc.name AS client_name,
                    COALESCE(rc.cnt, 0) AS rate_line_count,
                    COALESCE(rc.null_rate_count, 0) AS null_rate_count,
                    COALESCE(rc.null_site_count, 0) AS null_site_count,
                    COALESCE(rc.empty_billing_rules_count, 0) AS empty_billing_rules_count
                FROM contract_terms_version ctv
                JOIN billing_client bc ON bc.id = ctv.billing_client_id
                LEFT JOIN (
                    SELECT contract_terms_version_id,
                           count(*)::int AS cnt,
                           count(*) FILTER (WHERE rate_amount IS NULL)::int AS null_rate_count,
                           count(*) FILTER (WHERE service_site_id IS NULL)::int AS null_site_count,
                           count(*) FILTER (WHERE billing_rules IS NULL OR billing_rules::text IN ('{}', 'null', ''))::int AS empty_billing_rules_count
                    FROM contract_rate_line
                    GROUP BY contract_terms_version_id
                ) rc ON rc.contract_terms_version_id = ctv.id
                WHERE ((cardinality(CAST(:sts AS text[])) = 0) OR (ctv.status::text = ANY(CAST(:sts AS text[]))))
                  AND (
                    :q = ''
                    OR lower(COALESCE(ctv.title, '')) LIKE '%%' || :q || '%%'
                    OR lower(COALESCE(ctv.ref_number, '')) LIKE '%%' || :q || '%%'
                    OR lower(COALESCE(bc.name, '')) LIKE '%%' || :q || '%%'
                  )
                ORDER BY ctv.updated_at DESC
                LIMIT :lim OFFSET :off
                """),
                {"sts": sts, "q": q, "lim": ps, "off": offset},
            )
            items = [dict(row) for row in r2.mappings().all()]
    return {
        "count": len(items),
        "total": total,
        "page": p,
        "page_size": ps,
        "items": items,
    }


async def contract_review_get(*, contract_terms_version_id: str) -> dict[str, Any]:
    ctv_id = str(contract_terms_version_id)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                SELECT ctv.*, bc.name AS client_name
                FROM contract_terms_version ctv
                JOIN billing_client bc ON bc.id = ctv.billing_client_id
                WHERE ctv.id = CAST(:id AS uuid)
                """),
                {"id": ctv_id},
            )
            hdr = r.mappings().first()
            if not hdr:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contract_terms_version not found")
            hdr_d = dict(hdr)

            r2 = await session.execute(
                text("""
                SELECT *
                FROM contract_party
                WHERE contract_terms_version_id = CAST(:id AS uuid)
                ORDER BY party_role, id
                """),
                {"id": ctv_id},
            )
            parties = [dict(x) for x in r2.mappings().all()]

            r3 = await session.execute(
                text("""
                SELECT *
                FROM payment_terms
                WHERE contract_terms_version_id = CAST(:id AS uuid)
                """),
                {"id": ctv_id},
            )
            payment_terms = dict(r3.mappings().first() or {})

            r4 = await session.execute(
                text("""
                SELECT
                    crl.*,
                    COALESCE(ss.site_key, ss.canonical_name, ss.display_name, ss.id::text) AS service_site_key,
                    COALESCE(ss.display_name, ss.canonical_name, ss.site_key, ss.id::text) AS service_site_name,
                    row_number() OVER (
                        PARTITION BY
                            COALESCE(crl.role_code, ''),
                            COALESCE(crl.billing_model::text, ''),
                            COALESCE(crl.service_site_id::text, '')
                        ORDER BY crl.id
                    ) AS line_variant_no,
                    count(*) OVER (
                        PARTITION BY
                            COALESCE(crl.role_code, ''),
                            COALESCE(crl.billing_model::text, ''),
                            COALESCE(crl.service_site_id::text, '')
                    ) AS line_variant_count
                FROM contract_rate_line crl
                LEFT JOIN service_site ss ON ss.id = crl.service_site_id
                WHERE contract_terms_version_id = CAST(:id AS uuid)
                ORDER BY crl.service_site_id NULLS FIRST, crl.billing_model, crl.role_code, crl.id
                """),
                {"id": ctv_id},
            )
            rate_lines = [dict(x) for x in r4.mappings().all()]

            r5 = await session.execute(
                text("""
                SELECT cd.id, cd.original_filename, cd.folder_path, cd.created_at, ctd.doc_role
                FROM contract_terms_document ctd
                JOIN contract_document cd ON cd.id = ctd.contract_document_id
                WHERE ctd.contract_terms_version_id = CAST(:id AS uuid)
                ORDER BY cd.created_at DESC
                """),
                {"id": ctv_id},
            )
            documents = [dict(x) for x in r5.mappings().all()]

            r6 = await session.execute(
                text("""
                SELECT cer.*
                FROM contract_extraction_run cer
                JOIN contract_terms_document ctd ON ctd.contract_document_id = cer.contract_document_id
                WHERE ctd.contract_terms_version_id = CAST(:id AS uuid)
                ORDER BY cer.finished_at DESC NULLS LAST, cer.started_at DESC NULLS LAST
                LIMIT 1
                """),
                {"id": ctv_id},
            )
            extraction_run = dict(r6.mappings().first() or {})
    return {
        "header": hdr_d,
        "parties": parties,
        "payment_terms": payment_terms,
        "rate_lines": rate_lines,
        "documents": documents,
        "latest_extraction_run": extraction_run,
    }


async def contract_review_failures(*, limit: int, open_only: bool) -> dict[str, Any]:
    n = max(1, min(int(limit), 1000))
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            if open_only:
                r = await session.execute(
                    text("""
                    SELECT *
                    FROM failed_contract_parsing
                    WHERE resolved_at IS NULL
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """),
                    {"lim": n},
                )
            else:
                r = await session.execute(
                    text("""
                    SELECT *
                    FROM failed_contract_parsing
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """),
                    {"lim": n},
                )
            items = [dict(row) for row in r.mappings().all()]
    return {"count": len(items), "items": items}


def _rate_line_patch_fragments(item: ContractRateLinePatchItem) -> tuple[list[str], dict[str, Any]]:
    """Build SET fragments and bind params for one rate line update (whitelist columns)."""
    fragments: list[str] = []
    params: dict[str, Any] = {}
    n = 0

    def add_fragment(sql_expr: str, val: Any) -> None:
        nonlocal n
        pname = f"v{n}"
        n += 1
        params[pname] = val
        fragments.append(sql_expr.replace(":bind", f":{pname}"))

    if item.billing_model is not None:
        add_fragment("billing_model = :bind", item.billing_model.strip().lower())
    if item.role_code is not None:
        add_fragment("role_code = :bind", item.role_code[:200])
    if item.description is not None:
        add_fragment("description = :bind", item.description[:2000])
    if item.rate_amount is not None:
        add_fragment("rate_amount = :bind", item.rate_amount)
    if item.rate_unit is not None:
        add_fragment("rate_unit = :bind", item.rate_unit[:100])
    if item.contracted_quantity is not None:
        add_fragment("contracted_quantity = :bind", item.contracted_quantity)
    if item.attendance_required is not None:
        add_fragment("attendance_required = :bind", item.attendance_required)
    if item.minimum_units_per_period is not None:
        add_fragment("minimum_units_per_period = :bind", item.minimum_units_per_period)
    if item.unfilled_penalty_pct is not None:
        add_fragment("unfilled_penalty_pct = :bind", item.unfilled_penalty_pct)
    if item.ot_multiplier is not None:
        add_fragment("ot_multiplier = :bind", item.ot_multiplier)
    if item.service_charge_type is not None:
        add_fragment("service_charge_type = :bind", item.service_charge_type[:50])
    if item.service_charge_value is not None:
        add_fragment("service_charge_value = :bind", item.service_charge_value)
    if item.actuals_markup_pct is not None:
        add_fragment("actuals_markup_pct = :bind", item.actuals_markup_pct)
    if item.schedule_type is not None:
        add_fragment("schedule_type = :bind", item.schedule_type[:50])
    if item.schedule_config is not None:
        add_fragment("schedule_config = CAST(:bind AS jsonb)", json.dumps(item.schedule_config))
    if item.billing_rules is not None:
        add_fragment("billing_rules = CAST(:bind AS jsonb)", json.dumps(item.billing_rules))
    if item.billing_rule_text is not None:
        add_fragment("billing_rule_text = :bind", item.billing_rule_text[:20000])
    if item.currency is not None:
        add_fragment("currency = :bind", item.currency[:20])
    if item.effective_from is not None:
        ef = _parse_iso_date(item.effective_from)
        if ef is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"effective_from must be YYYY-MM-DD for line {item.id}",
            )
        add_fragment("effective_from = CAST(:bind AS date)", ef)
    if item.effective_to is not None:
        et = _parse_iso_date(item.effective_to)
        if et is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"effective_to must be YYYY-MM-DD for line {item.id}",
            )
        add_fragment("effective_to = CAST(:bind AS date)", et)
    if item.is_active is not None:
        add_fragment("is_active = :bind", bool(item.is_active))
    return fragments, params


async def contract_review_rate_lines_patch(
    *,
    contract_terms_version_id: str,
    items: list[ContractRateLinePatchItem],
) -> dict[str, Any]:
    if not items:
        return {"status": "ok", "updated_count": 0, "updated_ids": []}
    for item in items:
        validate_contract_rate_line_patch(item)
    ctv_id = str(contract_terms_version_id)
    updated_ids: list[str] = []
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            for item in items:
                chk = await session.execute(
                    text("""
                    SELECT id FROM contract_rate_line
                    WHERE id = CAST(:lid AS uuid) AND contract_terms_version_id = CAST(:ctv AS uuid)
                    """),
                    {"lid": str(item.id), "ctv": ctv_id},
                )
                if chk.mappings().first() is None:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        f"contract_rate_line {item.id} not found in contract_terms_version {ctv_id}",
                    )
                frags, extra = _rate_line_patch_fragments(item)
                if not frags:
                    continue
                params = {"lid": str(item.id), "ctv": ctv_id, **extra}
                sql = f"""
                UPDATE contract_rate_line
                SET {", ".join(frags)}
                WHERE id = CAST(:lid AS uuid) AND contract_terms_version_id = CAST(:ctv AS uuid)
                """
                res = await session.execute(text(sql), params)
                if (res.rowcount or 0) > 0:
                    updated_ids.append(str(item.id))
    return {"status": "ok", "updated_count": len(updated_ids), "updated_ids": updated_ids}


async def contract_review_set_status(
    *,
    contract_terms_version_id: str,
    status_value: str,
    updated_by: str,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict[str, Any]:
    """Contract Health cleanup only — ``approved`` is set via MIS Save & Approve."""
    st = status_value.strip().lower()
    if st in {"approved", "draft"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "contract approval is only via MIS Save & Approve; use expired or rejected here",
        )
    if st not in {"rejected", "expired"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid status; allowed: expired, rejected")
    ctv = str(contract_terms_version_id)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            if st == "expired" and period_start and period_end:
                ps, pe = _parse_period(period_start, period_end)
                await _validate_expire_safe_for_period(
                    session,
                    contract_terms_version_id=ctv,
                    period_start=ps,
                    period_end=pe,
                )
            upd = await session.execute(
                text("""
                UPDATE contract_terms_version
                SET status = :st, updated_at = now()
                WHERE id = CAST(:id AS uuid)
                """),
                {"st": st, "id": ctv},
            )
            if (upd.rowcount or 0) <= 0:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contract_terms_version not found")
    return {
        "status": "ok",
        "contract_terms_version_id": ctv,
        "contract_status": st,
        "updated_by": updated_by,
    }
