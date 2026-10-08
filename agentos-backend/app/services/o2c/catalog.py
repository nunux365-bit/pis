"""Read-only agenos catalog queries for O2C UI (sites, clients, rate lines)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text

from app.agents.o2c_ohc.agenos_async_session import AgenosAsyncSessionLocal
from app.services.o2c.mis_contract_lines import skip_global_crl_when_site_override_exists_sql


async def list_active_service_sites_for_picker(*, query: str | None, limit: int) -> dict[str, Any]:
    q = (query or "").strip().lower()
    n = max(1, min(int(limit), 500))
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                    SELECT
                        ss.id::text AS id,
                        ss.billing_client_id::text AS billing_client_id,
                        bc.name AS billing_client_name,
                        COALESCE(ss.site_key, ss.canonical_name, ss.display_name, ss.id::text) AS site_key,
                        COALESCE(ss.display_name, '') AS display_name,
                        COALESCE(ss.canonical_name, '') AS canonical_name
                    FROM service_site ss
                    JOIN billing_client bc ON bc.id = ss.billing_client_id
                    WHERE ss.is_active = TRUE
                      AND (
                        :q = ''
                        OR lower(COALESCE(ss.site_key, '')) LIKE '%%' || :q || '%%'
                        OR lower(COALESCE(ss.display_name, '')) LIKE '%%' || :q || '%%'
                        OR lower(COALESCE(ss.canonical_name, '')) LIKE '%%' || :q || '%%'
                        OR lower(COALESCE(bc.name, '')) LIKE '%%' || :q || '%%'
                      )
                    ORDER BY bc.name, site_key
                    LIMIT :lim
                    """),
                {"q": q, "lim": n},
            )
            items = [dict(row) for row in r.mappings().all()]
    return {"count": len(items), "items": items}


async def list_billing_clients(*, query: str, limit: int) -> dict[str, Any]:
    q = (query or "").strip().lower()
    n = max(1, min(int(limit), 500))
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r = await session.execute(
                text("""
                    SELECT id::text AS id, name
                    FROM billing_client
                    WHERE :q = '' OR lower(COALESCE(name, '')) LIKE '%%' || :q || '%%'
                    ORDER BY name
                    LIMIT :lim
                    """),
                {"q": q, "lim": n},
            )
            items = [dict(row) for row in r.mappings().all()]
    return {"count": len(items), "items": items}


async def list_available_rate_lines_for_mis_run(*, mis_run_id: UUID) -> dict[str, Any]:
    async with AgenosAsyncSessionLocal() as session:
        async with session.begin():
            r0 = await session.execute(
                text(
                    "SELECT contract_terms_version_id, service_site_id FROM o2c_mis_run "
                    "WHERE id = CAST(:mid AS uuid)"
                ),
                {"mid": str(mis_run_id)},
            )
            mr = r0.mappings().first()
            if not mr:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "MIS run not found")
            mr_d = dict(mr)

            skip_global = skip_global_crl_when_site_override_exists_sql(crl_alias="crl")
            r1 = await session.execute(
                text(f"""
                    SELECT crl.id::text, crl.description, crl.role_code, crl.billing_model,
                           crl.rate_amount, crl.contracted_quantity, crl.is_active,
                           crl.overrides_contract_rate_line_id::text AS overrides_contract_rate_line_id,
                           COALESCE(ss.display_name, '') AS service_site_name
                    FROM contract_rate_line crl
                    LEFT JOIN service_site ss ON ss.id = crl.service_site_id
                    WHERE crl.contract_terms_version_id = CAST(:ctv AS uuid)
                      AND (crl.service_site_id = CAST(:ssid AS uuid) OR crl.service_site_id IS NULL)
                      {skip_global}
                    ORDER BY crl.is_active DESC, crl.description, crl.role_code
                    """),
                {
                    "ctv": str(mr_d["contract_terms_version_id"]),
                    "ssid": str(mr_d["service_site_id"]),
                },
            )
            all_lines = [dict(row) for row in r1.mappings().all()]

            r2 = await session.execute(
                text(
                    "SELECT contract_rate_line_id::text AS contract_rate_line_id "
                    "FROM o2c_mis_summary_row WHERE mis_run_id = CAST(:mid AS uuid) AND is_omitted = false"
                ),
                {"mid": str(mis_run_id)},
            )
            used_ids = {row["contract_rate_line_id"] for row in r2.mappings().all()}

    for ln in all_lines:
        ln["already_in_summary"] = ln["id"] in used_ids
        ln["is_active"] = bool(ln.get("is_active"))
        if ln.get("rate_amount") is not None:
            ln["rate_amount"] = float(ln["rate_amount"])
        if ln.get("contracted_quantity") is not None:
            ln["contracted_quantity"] = float(ln["contracted_quantity"])

    return {"items": all_lines}
