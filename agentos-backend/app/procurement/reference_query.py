"""Query helpers for ``pr_po_reference_values`` (list + server-side search)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import String, and_, case, cast, false, func, or_, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import PrPoReferenceValue
from app.procurement.reference_domains import (
    REFERENCE_SEARCH_DEFAULT_LIMIT,
    REFERENCE_SEARCH_MAX_LIMIT,
    SEARCHABLE_REFERENCE_DOMAINS,
)
from app.procurement.tax_code_allowlist import tax_code_allowlist_predicate

# Vendor master can be large (~200k). Require a short query to avoid full scans;
# numeric SAP vendor ids can be narrowed with prefix match on split_part(code,'|',1).
VENDOR_SEARCH_MIN_CHARS: int = 2
ASSET_SEARCH_MIN_CHARS: int = 2


def workflow_material_product_types(document_type: str) -> tuple[str, ...] | None:
    """SAP ``ProductType`` values allowed for material lines per workflow doc type."""
    dt = (document_type or "").strip().upper()
    if dt == "YUNB":
        return ("YUNB",)
    if dt == "YAST":
        return ("YUNB", "YCAP")
    return None


def storage_location_plant_code(raw: str) -> str:
    v = str(raw or "").strip()
    if not v or "|" not in v:
        return ""
    return v.split("|", 1)[0].strip()


def storage_location_sloc_code(raw: str) -> str:
    """Sloc segment from header value (``H001|3021`` → ``3021``; bare ``3021`` unchanged)."""
    v = str(raw or "").strip()
    if not v:
        return ""
    if "|" in v:
        return v.split("|", 1)[1].strip()
    return v


def storage_location_matches_plant(*, storage_location: str, plant: str) -> bool:
    pl = str(plant or "").strip()
    sloc_raw = str(storage_location or "").strip()
    if not pl or not sloc_raw:
        return True
    seg = storage_location_plant_code(sloc_raw)
    if not seg:
        return False
    return seg.upper() == pl.upper()


def asset_public_code(code: str, extra: dict[str, Any] | None = None) -> str:
    """SAP ``MasterFixedAsset`` value for forms — bare ``Anln1``, not ``Bukrs|Anln1`` DB key."""
    if isinstance(extra, dict):
        an = (extra.get("asset_number") or "").strip()
        if an:
            return an
    c = (code or "").strip()
    if "|" in c:
        return c.split("|", 1)[1].strip()
    return c


def reference_list_filter(
    *,
    domain: str,
    workflow_document_type: str,
    ticket_kind: str | None,
) -> ColumnElement[bool]:
    """Filter rows for workflow document type (YSER/YUNB/YAST) and optional PR/PO kind."""
    dt = (workflow_document_type or "").strip().upper()
    kind_u = (ticket_kind or "").strip().upper()
    wf_match = or_(PrPoReferenceValue.document_type == "", PrPoReferenceValue.document_type == dt)
    if kind_u in ("PR", "PO"):
        kind_match = or_(PrPoReferenceValue.applies_to_kind == "", PrPoReferenceValue.applies_to_kind == kind_u)
    else:
        kind_match = PrPoReferenceValue.applies_to_kind == ""
    return wf_match & kind_match


def _vendor_number_column() -> Any:
    """SAP vendor number before ``|`` in composite ``code``."""
    return func.split_part(PrPoReferenceValue.code, "|", 1)


def _vendor_name_searchable() -> tuple[Any, Any, Any, Any]:
    """JSON text paths used for name / search term (no full-JSON cast)."""
    extra = PrPoReferenceValue.extra
    name1 = func.coalesce(extra["name_1"].as_string(), "")
    name2 = func.coalesce(extra["name_2"].as_string(), "")
    st = func.coalesce(extra["searchterm"].as_string(), "")
    city = func.coalesce(extra["city"].as_string(), "")
    return name1, name2, st, city


def _vendor_text_predicates(pat: str, q_core: str) -> ColumnElement[bool]:
    """ILIKE targets for vendor: composite code, display label, names, search term, city."""
    name1, name2, st, city = _vendor_name_searchable()
    vn = _vendor_number_column()
    q_digits = re.sub(r"\D", "", q_core)
    # Typing a SAP number: prefer prefix on vendor id (index-friendly when planner uses btree on code).
    if q_digits and (q_digits == q_core or q_core.isdigit()):
        return or_(
            vn == q_digits,
            vn.ilike(f"{q_digits}%"),
            PrPoReferenceValue.code.ilike(pat),
            PrPoReferenceValue.label.ilike(pat),
            name1.ilike(pat),
            name2.ilike(pat),
            st.ilike(pat),
            city.ilike(pat),
        )
    return or_(
        PrPoReferenceValue.code.ilike(pat),
        PrPoReferenceValue.label.ilike(pat),
        name1.ilike(pat),
        name2.ilike(pat),
        st.ilike(pat),
        city.ilike(pat),
    )


def _asset_text_predicates(pat: str, q_core: str) -> ColumnElement[bool]:
    ex = PrPoReferenceValue.extra
    anlkl = func.coalesce(ex["asset_class"].as_string(), "")
    asset_number = func.coalesce(ex["asset_number"].as_string(), "")
    preds: list[ColumnElement[bool]] = [
        PrPoReferenceValue.code.ilike(pat),
        PrPoReferenceValue.label.ilike(pat),
        anlkl.ilike(pat),
        asset_number.ilike(pat),
    ]
    digits = re.sub(r"\D", "", q_core)
    if digits:
        preds.append(asset_number.ilike(f"%{digits}%"))
        preds.append(PrPoReferenceValue.code.ilike(f"%{digits}%"))
    return or_(*preds)


def _asset_recency_column() -> Any:
    """SAP ``CreatedOn`` / ``ChangedOn`` (``YYYYMMDD`` text) for newest-first browse."""
    ex = PrPoReferenceValue.extra
    created = func.coalesce(ex["created_on"].as_string(), "")
    changed = func.coalesce(ex["changed_on"].as_string(), "")
    return func.coalesce(func.nullif(created, ""), func.nullif(changed, ""), "")


def _asset_search_order(q_core: str = "") -> list[Any]:
    """Asset picker: prefix relevance when searching, then newest SAP create/change date."""
    ex = PrPoReferenceValue.extra
    an = func.coalesce(ex["asset_number"].as_string(), PrPoReferenceValue.code)
    parts: list[Any] = []
    if q_core:
        parts.append(case((an.ilike(f"{q_core}%"), 0), else_=1).asc())
    parts.extend(
        [
            _asset_recency_column().desc(),
            PrPoReferenceValue.label.asc(),
            PrPoReferenceValue.code.desc(),
        ]
    )
    return parts


def _service_text_predicates(pat: str, q_core: str) -> ColumnElement[bool]:
    """Service code / label / short_code (QAS long SAP numbers + catalogue short codes)."""
    ex = PrPoReferenceValue.extra
    long_txt = func.coalesce(ex["Material Master Long Text"].as_string(), "")
    short_code = func.coalesce(ex["short_code"].as_string(), "")
    preds: list[ColumnElement[bool]] = [
        PrPoReferenceValue.code.ilike(pat),
        PrPoReferenceValue.label.ilike(pat),
        long_txt.ilike(pat),
        short_code.ilike(pat),
    ]
    digits = re.sub(r"\D", "", q_core)
    if digits:
        preds.append(short_code == digits)
        preds.append(PrPoReferenceValue.code.ilike(f"%{digits}%"))
        if len(digits) >= 6:
            preds.append(PrPoReferenceValue.code.ilike(f"%{digits[-10:]}%"))
    return or_(*preds)


def _non_vendor_text_predicates(domain: str, pat: str, q_core: str = "") -> ColumnElement[bool]:
    if domain == "material":
        ex = PrPoReferenceValue.extra
        mg = func.coalesce(ex["material_group"].as_string(), "")
        mt = func.coalesce(ex["material_type"].as_string(), "")
        ind = func.coalesce(ex["industry_sector"].as_string(), "")
        omt = func.coalesce(ex["old_material_number"].as_string(), "")
        desc = func.coalesce(ex["description"].as_string(), "")
        return or_(
            PrPoReferenceValue.code.ilike(pat),
            PrPoReferenceValue.label.ilike(pat),
            desc.ilike(pat),
            mg.ilike(pat),
            mt.ilike(pat),
            ind.ilike(pat),
            omt.ilike(pat),
        )
    if domain == "service":
        return _service_text_predicates(pat, q_core)
    if domain == "asset":
        return _asset_text_predicates(pat, q_core)
    json_txt = func.coalesce(cast(PrPoReferenceValue.extra, String), "")
    return or_(
        PrPoReferenceValue.code.ilike(pat),
        PrPoReferenceValue.label.ilike(pat),
        json_txt.ilike(pat),
    )


def _material_type_workflow_predicate(
    workflow_document_type: str,
) -> ColumnElement[bool]:
    """Material rows: only SAP product types valid for YUNB/YAST workflows."""
    mtypes = workflow_material_product_types(workflow_document_type)
    if not mtypes:
        return true()
    ex = PrPoReferenceValue.extra
    upper_mt = func.upper(func.coalesce(ex["material_type"].as_string(), ""))
    return upper_mt.in_([m.upper() for m in mtypes])


def _material_group_has_workflow_materials_predicate(
    workflow_document_type: str,
) -> ColumnElement[bool]:
    """Material group rows: at least one catalogue material matches workflow product type."""
    mtypes = workflow_material_product_types(workflow_document_type)
    if not mtypes:
        return true()
    mat = aliased(PrPoReferenceValue)
    ex = mat.extra
    upper_mt = func.upper(func.coalesce(ex["material_type"].as_string(), ""))
    return (
        select(1)
        .where(
            mat.domain == "material",
            func.coalesce(ex["material_group"].as_string(), "") == PrPoReferenceValue.code,
            upper_mt.in_([m.upper() for m in mtypes]),
        )
        .correlate(PrPoReferenceValue)
        .exists()
    )


def _storage_location_plant_predicate(plant: str) -> ColumnElement[bool]:
    """Storage locations scoped to header plant (``H001|3021`` or ``extra.plant``)."""
    pl = (plant or "").strip()
    if not pl:
        return true()
    ex = PrPoReferenceValue.extra
    return or_(
        PrPoReferenceValue.code.ilike(f"{pl}|%"),
        func.coalesce(ex["plant"].as_string(), "") == pl,
    )


def cost_center_plant_business_area_predicate(plant: str) -> ColumnElement[bool]:
    """Cost centre Business Area matches any storage-location sloc under ``plant``.

    Storage locations are ``plant|sloc``; cost centres store that sloc as Business Area.
    Empty ``plant`` → no extra restriction. Uses ``EXISTS`` (scales for plants with many slocs).
    """
    pl = (plant or "").strip()
    if not pl:
        return true()
    sloc = aliased(PrPoReferenceValue, name="cc_plant_sloc")
    sloc_code = func.split_part(sloc.code, "|", 2)
    ex = PrPoReferenceValue.extra
    ba_match = or_(
        *[func.coalesce(ex[k].as_string(), "") == sloc_code for k in FACET_BUSINESS_AREA_KEYS]
    )
    plant_match = or_(
        sloc.code.ilike(f"{pl}|%"),
        func.coalesce(sloc.extra["plant"].as_string(), "") == pl,
    )
    return (
        select(1)
        .where(
            sloc.domain == "storage_location",
            plant_match,
            func.coalesce(sloc_code, "") != "",
            ba_match,
        )
        .correlate(PrPoReferenceValue)
        .exists()
    )


def _search_base_where(
    *,
    domain: str,
    q: str,
    workflow_document_type: str,
    ticket_kind: str | None,
    company_code: str | None,
    material_group: str | None = None,
    service_group: str | None = None,
    cc_entity: str | None = None,
    cc_profit_center: str | None = None,
    cc_department: str | None = None,
    cc_business_area: str | None = None,
    plant: str | None = None,
) -> ColumnElement[bool]:
    q_core = (q or "").strip()
    pat = f"%{q_core}%"
    wf = reference_list_filter(
        domain=domain, workflow_document_type=workflow_document_type, ticket_kind=ticket_kind
    )
    parts: list[ColumnElement[bool]] = [wf, PrPoReferenceValue.domain == domain]
    if domain == "tax_code":
        parts.append(tax_code_allowlist_predicate(PrPoReferenceValue.code))

    plant_cc = (plant or "").strip() if domain == "cost_center" else ""
    facet_cc = bool(
        (cc_entity or "").strip()
        or (cc_profit_center or "").strip()
        or (cc_department or "").strip()
        or (cc_business_area or "").strip()
        or plant_cc
    )
    if domain == "vendor" and len(q_core) < VENDOR_SEARCH_MIN_CHARS:
        parts.append(false())
    elif domain == "asset" and not (company_code or "").strip():
        parts.append(false())
    elif domain == "asset" and q_core and len(q_core) < ASSET_SEARCH_MIN_CHARS:
        parts.append(false())
    elif domain == "cost_center" and not q_core and not facet_cc:
        parts.append(false())
    elif pat != "%%":
        if domain == "vendor":
            parts.append(_vendor_text_predicates(pat, q_core))
        else:
            parts.append(_non_vendor_text_predicates(domain, pat, q_core))
    elif domain == "cost_center" and facet_cc:
        parts.append(true())
    elif domain == "asset" and (company_code or "").strip():
        parts.append(true())
    elif domain in ("material", "service") and ((material_group or "").strip() or (service_group or "").strip()):
        parts.append(true())

    cc = (company_code or "").strip().upper()
    if domain == "asset" and cc:
        ex = PrPoReferenceValue.extra
        parts.append(
            or_(
                func.upper(func.coalesce(ex["company_code"].as_string(), "")) == cc,
                func.upper(PrPoReferenceValue.code).like(f"{cc}|%"),
            )
        )
    if domain == "vendor" and cc:
        # Exact CoCd segment (avoid ``%|1M`` matching ``…|1MGT``).
        cocd_seg = func.upper(func.nullif(func.split_part(PrPoReferenceValue.code, "|", 2), ""))
        parts.append(cocd_seg == cc)

    mg = (material_group or "").strip()
    if domain == "material" and mg:
        ex = PrPoReferenceValue.extra
        parts.append(func.coalesce(ex["material_group"].as_string(), "") == mg)

    sg = (service_group or "").strip()
    if domain == "service" and sg:
        ex = PrPoReferenceValue.extra
        parts.append(
            or_(
                func.coalesce(ex["service_group"].as_string(), "") == sg,
                func.coalesce(ex["Material Group"].as_string(), "") == sg,
            )
        )

    if domain == "material":
        parts.append(_material_type_workflow_predicate(workflow_document_type))
    if domain == "material_group":
        parts.append(_material_group_has_workflow_materials_predicate(workflow_document_type))
    if domain == "storage_location":
        parts.append(_storage_location_plant_predicate(plant or ""))

    if domain == "cost_center":
        ex = PrPoReferenceValue.extra

        def _cc_facet_match(json_keys: tuple[str, ...], needle: str) -> ColumnElement[bool]:
            t = f"%{needle.strip()}%"
            preds: list[ColumnElement[bool]] = []
            for k in json_keys:
                preds.append(func.coalesce(ex[k].as_string(), "").ilike(t))
            return or_(*preds) if preds else false()

        if (cc_entity or "").strip():
            parts.append(_cc_facet_match(("Entity", "entity", "Company"), cc_entity))
        if (cc_profit_center or "").strip():
            parts.append(_cc_facet_match(("Profit Center", "Profit center", "profit_center"), cc_profit_center))
        if (cc_department or "").strip():
            parts.append(_cc_facet_match(("Department", "department"), cc_department))
        # Plant union supersedes single-sloc BA: eligibility is any sloc under the plant.
        pl = (plant or "").strip()
        if pl:
            parts.append(cost_center_plant_business_area_predicate(pl))
        else:
            ba = (cc_business_area or "").strip()
            if ba:
                ba_preds: list[ColumnElement[bool]] = []
                for k in FACET_BUSINESS_AREA_KEYS:
                    ba_preds.append(func.coalesce(ex[k].as_string(), "") == ba)
                parts.append(or_(*ba_preds) if ba_preds else false())

    return and_(*parts)


def _vendor_relevance_order(q_core: str) -> list[Any]:
    """Best-effort ordering: exact / prefix vendor number, then prefix on label, then stable tie-break."""
    vn = _vendor_number_column()
    name1, _, _, _ = _vendor_name_searchable()
    q_digits = re.sub(r"\D", "", q_core)
    if q_digits and (q_digits == q_core or q_core.isdigit()):
        rank = case(
            (vn == q_digits, 0),
            (vn.ilike(f"{q_digits}%"), 1),
            (PrPoReferenceValue.label.ilike(f"{q_core}%"), 2),
            else_=3,
        )
    else:
        rank = case(
            (PrPoReferenceValue.label.ilike(f"{q_core}%"), 0),
            (name1.ilike(f"{q_core}%"), 1),
            (PrPoReferenceValue.code.ilike(f"{q_core}%"), 2),
            else_=3,
        )
    return [rank.asc(), PrPoReferenceValue.label.asc(), PrPoReferenceValue.code.asc()]


def reference_search_stmt(
    *,
    domain: str,
    q: str,
    workflow_document_type: str,
    ticket_kind: str | None,
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
) -> Select[Any]:
    """Build ORM select for paginated search."""
    lim = max(1, min(REFERENCE_SEARCH_MAX_LIMIT, limit))
    off = max(0, offset)
    where = _search_base_where(
        domain=domain,
        q=q,
        workflow_document_type=workflow_document_type,
        ticket_kind=ticket_kind,
        company_code=company_code,
        material_group=material_group,
        service_group=service_group,
        cc_entity=cc_entity,
        cc_profit_center=cc_profit_center,
        cc_department=cc_department,
        cc_business_area=cc_business_area,
        plant=plant,
    )
    q_core = (q or "").strip()
    stmt = select(PrPoReferenceValue).where(where)
    if domain == "vendor" and len(q_core) >= VENDOR_SEARCH_MIN_CHARS:
        stmt = stmt.order_by(*_vendor_relevance_order(q_core))
    elif domain == "asset":
        stmt = stmt.order_by(*_asset_search_order(q_core))
    else:
        stmt = stmt.order_by(PrPoReferenceValue.sort_order, PrPoReferenceValue.code)
    return stmt.offset(off).limit(lim)


async def reference_search_count(
    session: AsyncSession,
    *,
    domain: str,
    q: str,
    workflow_document_type: str,
    ticket_kind: str | None,
    company_code: str | None,
    material_group: str | None = None,
    service_group: str | None = None,
    cc_entity: str | None = None,
    cc_profit_center: str | None = None,
    cc_department: str | None = None,
    cc_business_area: str | None = None,
    plant: str | None = None,
) -> int:
    where = _search_base_where(
        domain=domain,
        q=q,
        workflow_document_type=workflow_document_type,
        ticket_kind=ticket_kind,
        company_code=company_code,
        material_group=material_group,
        service_group=service_group,
        cc_entity=cc_entity,
        cc_profit_center=cc_profit_center,
        cc_department=cc_department,
        cc_business_area=cc_business_area,
        plant=plant,
    )
    cnt_stmt = select(func.count()).select_from(PrPoReferenceValue).where(where)
    c = await session.scalar(cnt_stmt)
    return int(c or 0)


FACET_ENTITY_KEYS: tuple[str, ...] = ("Entity", "entity", "Company", "Controlling Area", "Controlling area")
FACET_PROFIT_CENTER_KEYS: tuple[str, ...] = (
    "Profit Center",
    "Profit center",
    "profit_center",
    "PC",
    "Profit Ctr",
)
FACET_DEPARTMENT_KEYS: tuple[str, ...] = ("Department", "department", "Dept", "Functional Area")
FACET_BUSINESS_AREA_KEYS: tuple[str, ...] = ("Business Area", "business_area")


_FACET_ROLE_LABELS: dict[str, str] = {
    "entity": "Company",
    "profit_center": "Profit centre",
    "department": "Department",
}


def _facet_option_label(v: str, *, facet_role: str) -> dict[str, str]:
    """``value`` is the stored facet token; ``label`` is what users see in dropdowns."""
    s = (v or "").strip()
    if not s:
        return {"value": "", "label": ""}
    role = (facet_role or "").strip().lower()
    role_hint = _FACET_ROLE_LABELS.get(role, "")
    codeish = len(s) <= 28 and not any(c.isspace() for c in s) and all(c.isalnum() or c in "_-./|" for c in s)
    if codeish and role_hint:
        return {"value": s, "label": f"{s} · {role_hint}"}
    return {"value": s, "label": s}


def _cc_extra_entity_row_matches(ex: Any, entity_scope: str) -> ColumnElement[bool]:
    """True when any canonical Entity / company JSON field equals ``entity_scope`` (case-insensitive)."""
    es = (entity_scope or "").strip()
    if not es:
        return true()
    nu = func.upper(es)
    preds = [func.upper(func.coalesce(ex[k].as_string(), "")) == nu for k in FACET_ENTITY_KEYS]
    return or_(*preds) if preds else false()


async def distinct_cost_center_facet_values(
    session: AsyncSession,
    *,
    document_type: str,
    ticket_kind: str | None,
    json_keys: tuple[str, ...],
    facet_role: str,
    entity_scope: str | None = None,
    limit: int = 500,
) -> list[dict[str, str]]:
    """Distinct non-empty ``extra`` JSON paths for cost_center rows (for searchable facet dropdowns).

    One DB round trip per facet role: per-key ``SELECT extra->>'key'`` is fanned out via
    ``UNION ALL`` and distinct/limited in the outer query. Previously we issued one query
    per JSON key which, combined with 3 facet roles, meant up to ~15 queries per page load.
    """
    dt = (document_type or "").strip().upper()
    kind_u = (ticket_kind or "").strip().upper()
    kind_param: str | None = kind_u if kind_u in ("PR", "PO") else None
    wf = reference_list_filter(domain="cost_center", workflow_document_type=dt, ticket_kind=kind_param)
    lim = max(1, min(800, int(limit)))
    ex = PrPoReferenceValue.extra
    es = (entity_scope or "").strip()

    common_conds: list[ColumnElement[bool]] = [
        PrPoReferenceValue.domain == "cost_center",
        wf,
        PrPoReferenceValue.extra.isnot(None),
    ]
    if es:
        common_conds.append(_cc_extra_entity_row_matches(ex, es))

    per_key_selects = []
    for jk in json_keys:
        col = ex[jk].as_string()
        per_key_selects.append(
            select(col.label("val")).where(
                and_(*common_conds, col.isnot(None), func.trim(col) != "")
            )
        )
    if not per_key_selects:
        return []
    u = union_all(*per_key_selects).subquery()
    stmt = select(u.c.val).distinct().order_by(u.c.val.asc()).limit(lim)
    res = await session.execute(stmt)
    acc: set[str] = set()
    for row in res.fetchall():
        v = row[0]
        if v is None:
            continue
        s = str(v).strip()
        if s:
            acc.add(s)
        if len(acc) >= lim:
            break
    out_str = sorted(acc)[:lim]
    return [_facet_option_label(x, facet_role=facet_role) for x in out_str]


async def cost_center_facet_options(
    session: AsyncSession,
    *,
    document_type: str,
    ticket_kind: str | None,
    purchasing_org: str | None = None,
) -> dict[str, Any]:
    """
    Entity / profit centre / department pick-lists from cost centre ``extra`` (HANA-style import).

    When ``purchasing_org`` is **sent** (including empty string), profit centre and department lists are
    restricted to cost centres whose entity/company fields match that purchasing organisation code.
    If the header value is blank, those lists are empty until the user selects a purchasing organisation.
    When ``purchasing_org`` is omitted, behaviour matches the legacy unscoped lists (all profit centres / departments).
    """
    strict = purchasing_org is not None
    po = (purchasing_org or "").strip()

    entity = await distinct_cost_center_facet_values(
        session,
        document_type=document_type,
        ticket_kind=ticket_kind,
        json_keys=FACET_ENTITY_KEYS,
        facet_role="entity",
        entity_scope=None,
    )

    matched: str | None = None
    if po:
        for row in entity:
            v = (row.get("value") or "").strip()
            if v and v.lower() == po.lower():
                matched = v
                break

    if strict:
        if not po or matched is None:
            profit_center: list[dict[str, str]] = []
            department = []
        else:
            profit_center = await distinct_cost_center_facet_values(
                session,
                document_type=document_type,
                ticket_kind=ticket_kind,
                json_keys=FACET_PROFIT_CENTER_KEYS,
                facet_role="profit_center",
                entity_scope=matched,
            )
            department = await distinct_cost_center_facet_values(
                session,
                document_type=document_type,
                ticket_kind=ticket_kind,
                json_keys=FACET_DEPARTMENT_KEYS,
                facet_role="department",
                entity_scope=matched,
            )
    else:
        profit_center = await distinct_cost_center_facet_values(
            session,
            document_type=document_type,
            ticket_kind=ticket_kind,
            json_keys=FACET_PROFIT_CENTER_KEYS,
            facet_role="profit_center",
            entity_scope=None,
        )
        department = await distinct_cost_center_facet_values(
            session,
            document_type=document_type,
            ticket_kind=ticket_kind,
            json_keys=FACET_DEPARTMENT_KEYS,
            facet_role="department",
            entity_scope=None,
        )

    facet_scope: dict[str, Any] | None = None
    if strict:
        facet_scope = {
            "resolved_entity": matched,
            "needs_purchasing_organisation": not bool(po),
            "no_directory_match_for_purchasing_org": bool(po) and matched is None,
        }

    return {
        "entity": entity,
        "profit_center": profit_center,
        "department": department,
        "has_rich_extra": bool(entity or profit_center or department),
        "facet_scope": facet_scope,
    }


def assert_searchable_domain(domain: str) -> None:
    if domain not in SEARCHABLE_REFERENCE_DOMAINS:
        raise ValueError(f"domain must be one of {sorted(SEARCHABLE_REFERENCE_DOMAINS)}")


def clamp_search_limit(limit: int | None) -> int:
    if limit is None:
        return REFERENCE_SEARCH_DEFAULT_LIMIT
    return max(1, min(REFERENCE_SEARCH_MAX_LIMIT, int(limit)))
