"""Contract Health dashboard: ops summary, client tree, siblings (read paths)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.agents.o2c_ohc.ingest_helpers import _parse_iso_date
from app.agents.o2c_ohc.terms_picker import (
    count_approved_billable_terms_async,
    list_billable_terms_for_site_async,
    pick_from_billable_rows,
)

_CTV_LINE_STATS_SQL = """
SELECT
    crl.contract_terms_version_id,
    count(*)::int AS rate_line_count,
    count(*) FILTER (WHERE crl.rate_amount IS NULL)::int AS null_rate_count,
    count(*) FILTER (WHERE crl.service_site_id IS NULL)::int AS null_site_count,
    count(*) FILTER (
        WHERE crl.billing_rules IS NULL OR crl.billing_rules::text IN ('{}', 'null', '')
    )::int AS empty_billing_rules_count
FROM contract_rate_line crl
WHERE crl.contract_terms_version_id = ANY(CAST(:ctv_ids AS uuid[]))
GROUP BY crl.contract_terms_version_id
"""

_CTV_SITE_LINES_SQL = """
SELECT
    crl.contract_terms_version_id,
    crl.service_site_id,
    count(*)::int AS line_count
FROM contract_rate_line crl
WHERE crl.contract_terms_version_id = ANY(CAST(:ctv_ids AS uuid[]))
  AND crl.is_active = true
GROUP BY crl.contract_terms_version_id, crl.service_site_id
"""


def _parse_period(period_start: str | None, period_end: str | None) -> tuple[date, date]:
    if not (period_start and period_end):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "period_start and period_end required (YYYY-MM-DD)")
    ps = _parse_iso_date(period_start)
    pe = _parse_iso_date(period_end)
    if ps is None or pe is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid period dates")
    if ps > pe:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "period_start must be <= period_end")
    return ps, pe


def build_mis_readiness_from_sites(sites: list[dict[str, Any]]) -> dict[str, Any]:
    """MIS draft generation blockers/warnings from client tree site rows."""
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    billable_site_count = 0

    for s in sites:
        terms = s.get("terms") or []
        if not terms:
            continue
        billable_site_count += 1
        sid = str(s.get("service_site_id") or "")
        name = str(s.get("site_name") or sid)

        if s.get("overlap_conflict"):
            blockers.append(
                {
                    "code": "overlap",
                    "service_site_id": sid,
                    "site_name": name,
                    "message": "Two or more approved contracts cover this billing month.",
                }
            )
        elif not s.get("picker_ctv_id"):
            blockers.append(
                {
                    "code": "no_billable_contract",
                    "service_site_id": sid,
                    "site_name": name,
                    "message": "No single billable contract for this billing month.",
                }
            )

        picker_id = s.get("picker_ctv_id")
        if picker_id:
            pick = next(
                (t for t in terms if str(t.get("contract_terms_version_id")) == str(picker_id)),
                None,
            )
            if pick:
                nr = int(pick.get("null_rate_count") or 0)
                ns = int(pick.get("null_site_count") or 0)
                if nr > 0 or ns > 0:
                    warnings.append(
                        {
                            "code": "data_issues",
                            "service_site_id": sid,
                            "site_name": name,
                            "contract_terms_version_id": str(picker_id),
                            "message": f"Billing contract has {nr} missing rate(s), {ns} unmapped line(s).",
                        }
                    )

    return {
        "ready": len(blockers) == 0,
        "billable_site_count": billable_site_count,
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "blockers": blockers,
        "warnings": warnings,
    }


async def _picker_ctv_id_for_site(
    session: AsyncSession,
    *,
    billing_client_id: str,
    service_site_id: str,
    period_start: date,
    period_end: date,
) -> str | None:
    rows = await list_billable_terms_for_site_async(
        session,
        billing_client_id,
        service_site_id,
        period_start=period_start,
        period_end=period_end,
    )
    picked = pick_from_billable_rows(rows)
    if not picked:
        return None
    return str(picked.get("contract_terms_version_id") or "")


async def contract_review_ops_summary(
    *,
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    ps, pe = _parse_period(period_start, period_end)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r_status = await session.execute(
                text("""
                SELECT lower(ctv.status::text) AS st, count(*)::int AS n
                FROM contract_terms_version ctv
                WHERE ctv.superseded_by_id IS NULL
                GROUP BY lower(ctv.status::text)
                """),
            )
            by_status = {str(row["st"]): int(row["n"]) for row in r_status.mappings().all()}

            r_defect = await session.execute(
                text("""
                WITH line_stats AS (
                    SELECT
                        crl.contract_terms_version_id,
                        count(*) FILTER (WHERE crl.rate_amount IS NULL)::int AS null_rates,
                        count(*) FILTER (WHERE crl.service_site_id IS NULL)::int AS null_sites,
                        count(*) FILTER (
                            WHERE crl.billing_rules IS NULL
                               OR crl.billing_rules::text IN ('{}', 'null', '')
                        )::int AS empty_rules
                    FROM contract_rate_line crl
                    WHERE crl.is_active = true
                    GROUP BY crl.contract_terms_version_id
                )
                SELECT
                    count(*) FILTER (
                        WHERE COALESCE(ls.null_rates, 0) > 0
                    )::int AS ctvs_with_null_rates,
                    count(*) FILTER (
                        WHERE COALESCE(ls.null_sites, 0) > 0
                    )::int AS ctvs_with_unmapped_sites,
                    count(*) FILTER (
                        WHERE COALESCE(ls.empty_rules, 0) > 0
                    )::int AS ctvs_with_empty_rules
                FROM contract_terms_version ctv
                LEFT JOIN line_stats ls ON ls.contract_terms_version_id = ctv.id
                WHERE ctv.superseded_by_id IS NULL
                  AND ctv.status IN ('pending', 'draft')
                """),
            )
            defects = dict(r_defect.mappings().first() or {})

            r_overlap = await session.execute(
                text("""
                WITH site_approved AS (
                    SELECT
                        ss.id AS service_site_id,
                        (
                            SELECT count(DISTINCT ctv2.id)::int
                            FROM contract_terms_version ctv2
                            WHERE ctv2.billing_client_id = ss.billing_client_id
                              AND ctv2.status = 'approved'
                              AND ctv2.superseded_by_id IS NULL
                              AND ctv2.effective_from <= CAST(:pe AS date)
                              AND (ctv2.effective_to IS NULL OR ctv2.effective_to >= CAST(:ps AS date))
                              AND EXISTS (
                                SELECT 1 FROM contract_rate_line crl2
                                WHERE crl2.contract_terms_version_id = ctv2.id
                                  AND crl2.is_active = true
                                  AND (
                                    crl2.service_site_id = ss.id
                                    OR crl2.service_site_id IS NULL
                                  )
                              )
                        ) AS approved_ctv_count
                    FROM service_site ss
                )
                SELECT count(*)::int AS site_count
                FROM site_approved
                WHERE approved_ctv_count >= 2
                """),
                {"ps": ps, "pe": pe},
            )
            overlap_sites = int((r_overlap.mappings().first() or {}).get("site_count") or 0)

            r_fail = await session.execute(
                text("""
                SELECT count(*)::int AS n
                FROM failed_contract_parsing
                WHERE resolved_at IS NULL
                """),
            )
            parse_failures = int((r_fail.mappings().first() or {}).get("n") or 0)

            r_overlap_rows = await session.execute(
                text("""
                SELECT
                    ss.id::text AS service_site_id,
                    bc.id::text AS billing_client_id,
                    bc.name AS client_name,
                    COALESCE(ss.display_name, ss.canonical_name, ss.site_key, ss.id::text) AS site_name,
                    (
                        SELECT count(DISTINCT ctv2.id)::int
                        FROM contract_terms_version ctv2
                        WHERE ctv2.billing_client_id = bc.id
                          AND ctv2.status = 'approved'
                          AND ctv2.superseded_by_id IS NULL
                          AND ctv2.effective_from <= CAST(:pe AS date)
                          AND (ctv2.effective_to IS NULL OR ctv2.effective_to >= CAST(:ps AS date))
                          AND EXISTS (
                            SELECT 1 FROM contract_rate_line crl2
                            WHERE crl2.contract_terms_version_id = ctv2.id
                              AND crl2.is_active = true
                              AND (
                                crl2.service_site_id = ss.id
                                OR crl2.service_site_id IS NULL
                              )
                          )
                    ) AS approved_ctv_count
                FROM service_site ss
                JOIN billing_client bc ON bc.id = ss.billing_client_id
                WHERE (
                    SELECT count(DISTINCT ctv3.id)::int
                    FROM contract_terms_version ctv3
                    WHERE ctv3.billing_client_id = bc.id
                      AND ctv3.status = 'approved'
                      AND ctv3.superseded_by_id IS NULL
                      AND ctv3.effective_from <= CAST(:pe AS date)
                      AND (ctv3.effective_to IS NULL OR ctv3.effective_to >= CAST(:ps AS date))
                      AND EXISTS (
                        SELECT 1 FROM contract_rate_line crl3
                        WHERE crl3.contract_terms_version_id = ctv3.id
                          AND crl3.is_active = true
                          AND (
                            crl3.service_site_id = ss.id
                            OR crl3.service_site_id IS NULL
                          )
                      )
                ) >= 2
                ORDER BY bc.name, site_name
                LIMIT 50
                """),
                {"ps": ps, "pe": pe},
            )
            overlap_queue = [dict(x) for x in r_overlap_rows.mappings().all()]

            r_pending_defect = await session.execute(
                text("""
                SELECT
                    ctv.id::text AS contract_terms_version_id,
                    ctv.title,
                    bc.id::text AS billing_client_id,
                    bc.name AS client_name,
                    ctv.status,
                    COALESCE(rc.null_rate_count, 0) AS null_rate_count,
                    COALESCE(rc.null_site_count, 0) AS null_site_count
                FROM contract_terms_version ctv
                JOIN billing_client bc ON bc.id = ctv.billing_client_id
                LEFT JOIN (
                    SELECT contract_terms_version_id,
                           count(*) FILTER (WHERE rate_amount IS NULL)::int AS null_rate_count,
                           count(*) FILTER (WHERE service_site_id IS NULL)::int AS null_site_count
                    FROM contract_rate_line
                    WHERE is_active = true
                    GROUP BY contract_terms_version_id
                ) rc ON rc.contract_terms_version_id = ctv.id
                WHERE ctv.superseded_by_id IS NULL
                  AND ctv.status IN ('pending', 'draft')
                  AND (
                    COALESCE(rc.null_rate_count, 0) > 0
                    OR COALESCE(rc.null_site_count, 0) > 0
                  )
                ORDER BY ctv.updated_at DESC
                LIMIT 50
                """),
            )
            data_issues = [dict(x) for x in r_pending_defect.mappings().all()]

    return {
        "period_start": ps.isoformat(),
        "period_end": pe.isoformat(),
        "by_status": by_status,
        "ctvs_with_null_rates": int(defects.get("ctvs_with_null_rates") or 0),
        "ctvs_with_unmapped_sites": int(defects.get("ctvs_with_unmapped_sites") or 0),
        "ctvs_with_empty_rules": int(defects.get("ctvs_with_empty_rules") or 0),
        "overlap_site_count": overlap_sites,
        "parse_failure_count": parse_failures,
        "queues": {
            "overlap_sites": overlap_queue,
            "pending_data_issues": data_issues,
        },
    }


async def contract_review_client_tree(
    *,
    billing_client_id: str,
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    bc = str(billing_client_id).strip()
    ps, pe = _parse_period(period_start, period_end)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            bc_r = await session.execute(
                text("SELECT id::text AS id, name FROM billing_client WHERE id = CAST(:id AS uuid)"),
                {"id": bc},
            )
            bc_row = bc_r.mappings().first()
            if not bc_row:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "billing_client not found")

            sites_r = await session.execute(
                text("""
                SELECT id::text AS service_site_id,
                       COALESCE(display_name, canonical_name, site_key, id::text) AS site_name,
                       site_key
                FROM service_site
                WHERE billing_client_id = CAST(:bc AS uuid)
                ORDER BY site_name
                """),
                {"bc": bc},
            )
            sites = [dict(x) for x in sites_r.mappings().all()]

            ctv_r = await session.execute(
                text("""
                SELECT
                    ctv.id::text AS contract_terms_version_id,
                    ctv.title,
                    ctv.status,
                    ctv.effective_from,
                    ctv.effective_to,
                    ctv.created_at
                FROM contract_terms_version ctv
                WHERE ctv.billing_client_id = CAST(:bc AS uuid)
                  AND ctv.superseded_by_id IS NULL
                  AND ctv.status NOT IN ('rejected')
                ORDER BY ctv.effective_from DESC NULLS LAST, ctv.created_at DESC
                """),
                {"bc": bc},
            )
            ctvs = [dict(x) for x in ctv_r.mappings().all()]
            ctv_ids = [c["contract_terms_version_id"] for c in ctvs]
            if not ctv_ids:
                empty_sites = [
                    {**s, "terms": [], "overlap_conflict": False, "picker_ctv_id": None} for s in sites
                ]
                return {
                    "billing_client_id": bc,
                    "client_name": bc_row["name"],
                    "period_start": ps.isoformat(),
                    "period_end": pe.isoformat(),
                    "global_terms": [],
                    "sites": empty_sites,
                    "mis_readiness": build_mis_readiness_from_sites(empty_sites),
                }

            stats_r = await session.execute(text(_CTV_LINE_STATS_SQL), {"ctv_ids": ctv_ids})
            stats_by_ctv = {str(r["contract_terms_version_id"]): dict(r) for r in stats_r.mappings().all()}

            site_lines_r = await session.execute(text(_CTV_SITE_LINES_SQL), {"ctv_ids": ctv_ids})
            site_ids_by_ctv: dict[str, set[str]] = {}
            global_ctv_ids: set[str] = set()
            for row in site_lines_r.mappings().all():
                ctv_id = str(row["contract_terms_version_id"])
                sid = row["service_site_id"]
                if sid is None:
                    global_ctv_ids.add(ctv_id)
                else:
                    site_ids_by_ctv.setdefault(ctv_id, set()).add(str(sid))

            def _enrich(ctv: dict[str, Any]) -> dict[str, Any]:
                cid = ctv["contract_terms_version_id"]
                st = stats_by_ctv.get(cid, {})
                return {
                    **ctv,
                    "effective_from": ctv["effective_from"].isoformat() if ctv.get("effective_from") else None,
                    "effective_to": ctv["effective_to"].isoformat() if ctv.get("effective_to") else None,
                    "created_at": ctv["created_at"].isoformat() if ctv.get("created_at") else None,
                    "rate_line_count": int(st.get("rate_line_count") or 0),
                    "null_rate_count": int(st.get("null_rate_count") or 0),
                    "null_site_count": int(st.get("null_site_count") or 0),
                    "empty_billing_rules_count": int(st.get("empty_billing_rules_count") or 0),
                }

            enriched = [_enrich(c) for c in ctvs]
            global_terms = [c for c in enriched if c["contract_terms_version_id"] in global_ctv_ids]

            site_payload: list[dict[str, Any]] = []
            for s in sites:
                sid = s["service_site_id"]
                terms = [
                    c
                    for c in enriched
                    if sid in site_ids_by_ctv.get(c["contract_terms_version_id"], set())
                ]
                approved_n = await count_approved_billable_terms_async(
                    session, bc, sid, period_start=ps, period_end=pe
                )
                picker_id = await _picker_ctv_id_for_site(
                    session, billing_client_id=bc, service_site_id=sid, period_start=ps, period_end=pe
                )
                site_payload.append(
                    {
                        **s,
                        "terms": terms,
                        "overlap_conflict": approved_n >= 2,
                        "picker_ctv_id": picker_id,
                    }
                )

            mis_readiness = build_mis_readiness_from_sites(site_payload)

    return {
        "billing_client_id": bc,
        "client_name": bc_row["name"],
        "period_start": ps.isoformat(),
        "period_end": pe.isoformat(),
        "global_terms": global_terms,
        "sites": site_payload,
        "mis_readiness": mis_readiness,
    }


async def contract_review_siblings(
    *,
    contract_terms_version_id: str,
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    ctv_id = str(contract_terms_version_id)
    ps, pe = _parse_period(period_start, period_end)
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            base_r = await session.execute(
                text("""
                SELECT ctv.id::text AS id, ctv.billing_client_id::text AS billing_client_id, ctv.title, ctv.status
                FROM contract_terms_version ctv
                WHERE ctv.id = CAST(:id AS uuid)
                """),
                {"id": ctv_id},
            )
            base = base_r.mappings().first()
            if not base:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contract_terms_version not found")

            site_r = await session.execute(
                text("""
                SELECT DISTINCT crl.service_site_id::text AS service_site_id
                FROM contract_rate_line crl
                WHERE crl.contract_terms_version_id = CAST(:id AS uuid)
                  AND crl.is_active = true
                  AND crl.service_site_id IS NOT NULL
                """),
                {"id": ctv_id},
            )
            site_ids = [str(r["service_site_id"]) for r in site_r.mappings().all() if r.get("service_site_id")]

            if not site_ids:
                return {
                    "contract_terms_version_id": ctv_id,
                    "period_start": ps.isoformat(),
                    "period_end": pe.isoformat(),
                    "items": [],
                }

            sib_r = await session.execute(
                text("""
                SELECT DISTINCT
                    ctv.id::text AS contract_terms_version_id,
                    ctv.title,
                    ctv.status,
                    ctv.effective_from,
                    ctv.effective_to,
                    ctv.created_at,
                    count(crl.id)::int AS rate_line_count
                FROM contract_terms_version ctv
                JOIN contract_rate_line crl ON crl.contract_terms_version_id = ctv.id
                    AND crl.is_active = true
                WHERE ctv.billing_client_id = CAST(:bc AS uuid)
                  AND ctv.id <> CAST(:id AS uuid)
                  AND ctv.superseded_by_id IS NULL
                  AND ctv.status NOT IN ('rejected')
                  AND crl.service_site_id = ANY(CAST(:site_ids AS uuid[]))
                GROUP BY ctv.id, ctv.title, ctv.status, ctv.effective_from, ctv.effective_to, ctv.created_at
                ORDER BY ctv.created_at DESC
                """),
                {"bc": base["billing_client_id"], "id": ctv_id, "site_ids": site_ids},
            )
            items = []
            for row in sib_r.mappings().all():
                d = dict(row)
                ef = d.get("effective_from")
                et = d.get("effective_to")
                overlaps = (
                    ef is not None
                    and ef <= pe
                    and (et is None or et >= ps)
                )
                items.append(
                    {
                        **d,
                        "effective_from": ef.isoformat() if ef else None,
                        "effective_to": et.isoformat() if et else None,
                        "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
                        "overlaps_period": overlaps,
                        "is_current": False,
                    }
                )

    return {
        "contract_terms_version_id": ctv_id,
        "period_start": ps.isoformat(),
        "period_end": pe.isoformat(),
        "items": items,
    }


async def contract_review_patch_header(
    *,
    contract_terms_version_id: str,
    effective_from: str | None = None,
    effective_to: str | None = None,
    clear_effective_to: bool = False,
    patch_effective_from: bool = False,
    patch_effective_to: bool = False,
) -> dict[str, Any]:
    ctv_id = str(contract_terms_version_id)
    if not patch_effective_from and not patch_effective_to and not clear_effective_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no header fields to update")

    ef: date | None = None
    if patch_effective_from:
        if not effective_from:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "effective_from required when patching")
        ef = _parse_iso_date(effective_from)
        if ef is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid effective_from")

    et: date | None = None
    if patch_effective_to and not clear_effective_to:
        if not effective_to:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "effective_to required when patching end date")
        et = _parse_iso_date(effective_to)
        if et is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid effective_to")

    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            cur = await session.execute(
                text("""
                SELECT effective_from, effective_to, status
                FROM contract_terms_version WHERE id = CAST(:id AS uuid)
                """),
                {"id": ctv_id},
            )
            row = cur.mappings().first()
            if not row:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contract_terms_version not found")
            st = str(row.get("status") or "").lower()
            if st == "approved":
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "cannot edit effective dates on approved terms; adjust via MIS or expire first",
                )
            if patch_effective_from:
                if ef is None:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "effective_from required when patching")
                new_from = ef
            else:
                new_from = row["effective_from"]

            if clear_effective_to:
                new_to = None
            elif patch_effective_to:
                new_to = et
            else:
                new_to = row["effective_to"]

            if new_from and new_to and new_from > new_to:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "effective_from must be <= effective_to")

            await session.execute(
                text("""
                UPDATE contract_terms_version
                SET effective_from = CAST(:ef AS date),
                    effective_to = CAST(:et AS date),
                    updated_at = now()
                WHERE id = CAST(:id AS uuid)
                """),
                {"id": ctv_id, "ef": new_from, "et": new_to},
            )
    return {
        "status": "ok",
        "contract_terms_version_id": ctv_id,
        "effective_from": new_from.isoformat() if new_from else None,
        "effective_to": new_to.isoformat() if new_to else None,
    }
