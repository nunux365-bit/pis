"""Reference master-data reads for procurement (called from API routes only)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PrPoReferenceValue
from app.procurement.field_schema import DOCUMENT_TYPE_CODES
from app.procurement.plant_org import plant_matches_purchasing_org
from app.procurement.reference_query import (
    asset_public_code,
    clamp_search_limit,
    cost_center_facet_options,
    reference_list_filter,
    reference_search_count,
    reference_search_stmt,
    _material_group_has_workflow_materials_predicate,
    _storage_location_plant_predicate,
)
from app.procurement.tax_code_allowlist import tax_code_allowlist_predicate


async def list_reference_value_dicts(
    session: AsyncSession,
    *,
    domain: str,
    document_type: str | None,
    ticket_kind: str | None,
    purchasing_org: str | None = None,
    plant: str | None = None,
) -> list[dict[str, Any]]:
    dt = (document_type or "").strip().upper()
    if not dt:
        raise ValueError("document_type is required")
    kind_u = (ticket_kind or "").strip().upper()
    kind_param: str | None = kind_u if kind_u in ("PR", "PO") else None
    if domain == "purchasing_doc_type" and kind_param not in ("PR", "PO"):
        raise ValueError(
            "ticket_kind query parameter (PR or PO) is required when domain is purchasing_doc_type"
        )
    q = select(PrPoReferenceValue).where(PrPoReferenceValue.domain == domain)
    q = q.where(reference_list_filter(domain=domain, workflow_document_type=dt, ticket_kind=kind_param))
    if domain == "tax_code":
        q = q.where(tax_code_allowlist_predicate(PrPoReferenceValue.code))
    if domain == "material_group":
        q = q.where(_material_group_has_workflow_materials_predicate(dt))
    if domain == "storage_location":
        if not (plant or "").strip():
            return []
        q = q.where(_storage_location_plant_predicate(plant or ""))
    q = q.order_by(PrPoReferenceValue.sort_order, PrPoReferenceValue.code)
    res = await session.execute(q)
    rows = res.scalars().all()
    po = (purchasing_org or "").strip()
    out: list[dict[str, Any]] = []
    for r in rows:
        if domain == "plant" and po and not plant_matches_purchasing_org(r.code, po):
            continue
        out.append(
            {
                "code": r.code,
                "label": r.label,
                "document_type": r.document_type or None,
                "applies_to_kind": r.applies_to_kind or None,
                "extra": r.extra,
            }
        )
    return out


async def cost_center_facet_options_payload(
    session: AsyncSession,
    *,
    document_type: str,
    ticket_kind: str | None,
    purchasing_org: str | None = None,
) -> dict[str, Any]:
    dt = (document_type or "").strip().upper()
    if dt not in DOCUMENT_TYPE_CODES:
        raise ValueError("document_type must be YSER, YUNB, or YAST")
    kind_u = (ticket_kind or "").strip().upper()
    kind_param: str | None = kind_u if kind_u in ("PR", "PO") else None
    return await cost_center_facet_options(
        session, document_type=dt, ticket_kind=kind_param, purchasing_org=purchasing_org
    )


async def search_reference_values_payload(
    session: AsyncSession,
    *,
    domain: str,
    document_type: str,
    ticket_kind: str | None,
    q: str,
    company_code: str | None,
    limit: int,
    offset: int,
    material_group: str | None = None,
    service_group: str | None = None,
    cc_entity: str | None = None,
    cc_profit_center: str | None = None,
    cc_department: str | None = None,
    cc_business_area: str | None = None,
    plant: str | None = None,
) -> dict[str, Any]:
    dt = (document_type or "").strip().upper()
    if dt not in DOCUMENT_TYPE_CODES:
        raise ValueError("document_type must be YSER, YUNB, or YAST")
    kind_u = (ticket_kind or "").strip().upper()
    kind_param: str | None = kind_u if kind_u in ("PR", "PO") else None
    lim = clamp_search_limit(limit)
    total = await reference_search_count(
        session,
        domain=domain,
        q=q,
        workflow_document_type=dt,
        ticket_kind=kind_param,
        company_code=company_code,
        material_group=material_group,
        service_group=service_group,
        cc_entity=cc_entity,
        cc_profit_center=cc_profit_center,
        cc_department=cc_department,
        cc_business_area=cc_business_area,
        plant=plant,
    )
    stmt = reference_search_stmt(
        domain=domain,
        q=q,
        workflow_document_type=dt,
        ticket_kind=kind_param,
        company_code=company_code,
        limit=lim,
        offset=offset,
        material_group=material_group,
        service_group=service_group,
        cc_entity=cc_entity,
        cc_profit_center=cc_profit_center,
        cc_department=cc_department,
        cc_business_area=cc_business_area,
        plant=plant,
    )
    res = await session.execute(stmt)
    rows = res.scalars().all()
    def _item_code(row: PrPoReferenceValue) -> str:
        if domain == "asset":
            ex = row.extra if isinstance(row.extra, dict) else None
            return asset_public_code(row.code, ex)
        return row.code

    def _item_label(row: PrPoReferenceValue) -> str:
        if domain != "material":
            return row.label
        ex = row.extra if isinstance(row.extra, dict) else {}
        mt = str(ex.get("material_type") or "").strip()
        if mt and mt.upper() not in row.label.upper():
            return f"{row.label} [{mt}]"
        return row.label

    return {
        "items": [
            {
                "code": _item_code(r),
                "label": _item_label(r),
                "description": r.label,
                "document_type": r.document_type or None,
                "applies_to_kind": r.applies_to_kind or None,
                "extra": r.extra,
            }
            for r in rows
        ],
        "total": total,
        "limit": lim,
        "offset": offset,
    }
