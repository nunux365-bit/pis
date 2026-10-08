"""Validate ticket forms against synced ``pr_po_reference_values`` (scoped master)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PrPoReferenceValue
from app.procurement.line_catalog_group import line_catalog_group
from app.procurement.reference_query import (
    FACET_ENTITY_KEYS,
    cost_center_plant_business_area_predicate,
    storage_location_sloc_code,
    workflow_material_product_types,
)
from app.procurement.line_tax_code import line_tax_code
from app.procurement.tax_code_allowlist import tax_code_is_allowed


def _norm(v: Any) -> str:
    return str(v or "").strip()


def _vendor_public_code(code: str) -> str:
    c = _norm(code)
    if "|" in c:
        return c.split("|", 1)[0].strip()
    return c


async def apply_vendor_payment_terms_from_catalogue(
    session: AsyncSession,
    *,
    form: dict[str, Any],
    kind: str,
) -> None:
    """PO header: fill ``payment_terms`` from vendor ``extra.payt`` when empty."""
    if (kind or "").upper() != "PO":
        return
    header = form.get("header")
    if not isinstance(header, dict):
        return
    if _norm(header.get("payment_terms")):
        return
    vendor_raw = _norm(header.get("vendor"))
    if not vendor_raw:
        return
    org = _norm(header.get("purchasing_org") or header.get("company_code"))
    keys = [vendor_raw]
    if "|" not in vendor_raw and org:
        keys.append(f"{_vendor_public_code(vendor_raw)}|{org}")
    for key in keys:
        res = await session.execute(
            select(PrPoReferenceValue.extra).where(
                PrPoReferenceValue.domain == "vendor",
                PrPoReferenceValue.code == key,
            )
        )
        row = res.scalar_one_or_none()
        if not isinstance(row, dict):
            continue
        payt = _norm(row.get("payt") or row.get("payment_terms"))
        if payt:
            header["payment_terms"] = payt
            return


async def validate_form_against_catalogue(
    session: AsyncSession,
    *,
    kind: str,
    document_type: str,
    form: dict[str, Any],
) -> list[str]:
    """DB-backed checks after ``validate_form`` (material type, vendor payt, CC entity)."""
    dt = (document_type or "").upper()
    kind_u = (kind or "").upper()
    header = form.get("header") if isinstance(form.get("header"), dict) else {}
    lines = form.get("lines") if isinstance(form.get("lines"), list) else []
    errs: list[str] = []
    org = _norm(header.get("purchasing_org") or header.get("company_code"))
    plant = _norm(header.get("plant"))
    sloc_ba = storage_location_sloc_code(_norm(header.get("storage_location")))
    mtypes = workflow_material_product_types(dt)

    tax_code = _norm(header.get("tax_code"))
    if tax_code and not tax_code_is_allowed(tax_code):
        errs.append(
            f"Header field “Tax code” ({tax_code}) is not in the procurement tax-code allowlist."
        )

    for bi, raw in enumerate(lines):
        if not isinstance(raw, dict):
            continue
        tc = line_tax_code(raw, header)
        if tc and not tax_code_is_allowed(tc):
            errs.append(
                f"Block {bi + 1}: “Tax code” ({tc}) is not in the procurement tax-code allowlist."
            )

    if kind_u == "PO":
        vendor = _norm(header.get("vendor"))
        if vendor:
            payt = _norm(header.get("payment_terms"))
            if not payt:
                keys = [vendor]
                if "|" not in vendor and org:
                    keys.append(f"{_vendor_public_code(vendor)}|{org}")
                found_payt = False
                for key in keys:
                    res = await session.execute(
                        select(PrPoReferenceValue.extra).where(
                            PrPoReferenceValue.domain == "vendor",
                            PrPoReferenceValue.code == key,
                        )
                    )
                    ex = res.scalar_one_or_none()
                    if isinstance(ex, dict) and _norm(ex.get("payt") or ex.get("payment_terms")):
                        found_payt = True
                        break
                if found_payt:
                    errs.append(
                        "Header field “Payment terms” is required for purchase orders "
                        "(select the vendor again to fill from master data)."
                    )

    for bi, raw in enumerate(lines):
        if not isinstance(raw, dict):
            continue
        bix = bi + 1
        if dt in ("YUNB", "YAST") and mtypes:
            mat = _norm(raw.get("material"))
            if not mat:
                continue
            res = await session.execute(
                select(PrPoReferenceValue.extra).where(
                    PrPoReferenceValue.domain == "material",
                    PrPoReferenceValue.code == mat,
                )
            )
            ex = res.scalar_one_or_none()
            if isinstance(ex, dict):
                mt = _norm(ex.get("material_type")).upper()
                if mt and mt not in mtypes:
                    errs.append(
                        f"Block {bix}: material {mat} has SAP type {mt} — "
                        f"use a {', '.join(mtypes)} material for {dt} documents."
                    )
                line_mg = _norm(ex.get("material_group"))
                expected_mg = line_catalog_group(raw, header, dt)
                if expected_mg and line_mg and line_mg != expected_mg:
                    errs.append(
                        f"Block {bix}: material {mat} is not in material group {expected_mg}."
                    )
            elif mat:
                errs.append(f"Block {bix}: material {mat} is not in the reference catalogue.")

        # YAST uses assets, not cost centres — skip CC org/plant checks.
        if dt == "YAST":
            continue
        # YUNB / YSER: org + plant (any sloc under plant) cost-centre scope.
        allocs = raw.get("allocations")
        if not isinstance(allocs, list) or not org:
            continue
        for ai, arow in enumerate(allocs):
            if not isinstance(arow, dict):
                continue
            cc = _norm(arow.get("cost_center"))
            if not cc:
                continue
            if not await _cost_center_matches_org(session, cc=cc, org=org):
                errs.append(
                    f"Block {bix}, allocation {ai + 1}: cost centre {cc} "
                    f"is not valid for purchasing organisation {org}."
                )
                continue
            if plant:
                if not await _cost_center_matches_plant(session, cc=cc, plant=plant):
                    errs.append(
                        f"Block {bix}, allocation {ai + 1}: cost centre {cc} "
                        f"is not valid for plant {plant} "
                        f"(business area must match a storage location under this plant)."
                    )
            elif sloc_ba and not await _cost_center_matches_business_area(
                session, cc=cc, business_area=sloc_ba
            ):
                # Legacy fallback when plant is unset but storage location is present.
                errs.append(
                    f"Block {bix}, allocation {ai + 1}: cost centre {cc} "
                    f"is not valid for storage location {sloc_ba} "
                    f"(business area must match sloc)."
                )

    return errs


async def _cost_center_matches_plant(
    session: AsyncSession, *, cc: str, plant: str
) -> bool:
    pl = _norm(plant)
    if not pl:
        return True
    res = await session.execute(
        select(PrPoReferenceValue.id).where(
            PrPoReferenceValue.domain == "cost_center",
            PrPoReferenceValue.code == cc,
            cost_center_plant_business_area_predicate(pl),
        )
    )
    return res.scalar_one_or_none() is not None


async def _cost_center_matches_business_area(
    session: AsyncSession, *, cc: str, business_area: str
) -> bool:
    ba = _norm(business_area)
    if not ba:
        return True
    ex = PrPoReferenceValue.extra
    conds = [func.coalesce(ex[k].as_string(), "") == ba for k in ("Business Area", "business_area")]
    res = await session.execute(
        select(PrPoReferenceValue.id).where(
            PrPoReferenceValue.domain == "cost_center",
            PrPoReferenceValue.code == cc,
            or_(*conds) if conds else False,
        )
    )
    return res.scalar_one_or_none() is not None


async def _cost_center_matches_org(
    session: AsyncSession, *, cc: str, org: str
) -> bool:
    ex = PrPoReferenceValue.extra
    conds = [func.upper(func.coalesce(ex[k].as_string(), "")) == org.upper() for k in FACET_ENTITY_KEYS]
    res = await session.execute(
        select(PrPoReferenceValue.id).where(
            PrPoReferenceValue.domain == "cost_center",
            PrPoReferenceValue.code == cc,
            or_(*conds) if conds else False,
        )
    )
    return res.scalar_one_or_none() is not None
