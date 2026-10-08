"""Vendor master fetch — delta on ``A_Supplier``, enrich via ``A_SupplierCompany``."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import select

from app.db.models import PrPoReferenceValue
from app.db.session import AsyncSessionLocal
from app.procurement.reference_sync import dates as sync_dates
from app.procurement.reference_sync.constants import (
    SUPPLIER_COLLECTION_PATH,
    VENDOR_COLLECTION_PATH,
    VENDOR_DELTA_SUPPLIER_BATCH,
    VENDOR_NAME_SUPPLIER_BATCH,
    VENDOR_SHARD_PAGE_SIZE,
    VENDOR_UNFILTERED_PAGE_SIZE,
    odata_page_size_for,
)
from app.procurement.reference_sync.odata_client import ODataClient
from app.procurement.sap_odata_utils import odata_entity_properties, odata_text

log = logging.getLogger(__name__)

_DEFAULT_COMPANY_CODES: tuple[str, ...] = ("1MGH", "1MGT", "1LFS")

# Narrow select keeps shard pages small; matches delta fetch shape.
_VENDOR_COMPANY_SELECT = (
    "Supplier,CompanyCode,CompanyCodeName,PaymentTerms,AccountingClerk"
)


async def vendor_company_codes_for_sync() -> list[str]:
    """Purchasing orgs + company codes from DB (JSON fixed master + prior imports)."""
    codes: set[str] = set(_DEFAULT_COMPANY_CODES)
    async with AsyncSessionLocal() as session:
        for domain in ("purchasing_org", "company_code"):
            rows = await session.execute(
                select(PrPoReferenceValue.code).where(
                    PrPoReferenceValue.domain == domain,
                    PrPoReferenceValue.code != "",
                )
            )
            for (code,) in rows.all():
                c = str(code or "").strip()
                if c:
                    codes.add(c)
    return sorted(codes)


def _odata_literal(value: str) -> str:
    """Single-quoted OData string literal (escape embedded quotes)."""
    return "'" + value.replace("'", "''") + "'"


def supplier_or_filter(supplier_ids: list[str]) -> str:
    """OData ``$filter`` OR over ``Supplier eq '…'`` (caller batches for URL limits)."""
    parts = [f"Supplier eq {_odata_literal(s)}" for s in supplier_ids if s]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "(" + " or ".join(parts) + ")"


async def fetch_delta_supplier_ids(
    odata: ODataClient,
    *,
    day: date,
) -> list[str]:
    """``A_Supplier`` rows created on ``day`` (UTC calendar day)."""
    date_filter = sync_dates.vendor_daily_filter(day)
    filt = f"({date_filter}) and Supplier ne ''"
    props = await odata.fetch_properties(
        SUPPLIER_COLLECTION_PATH,
        params={
            "$filter": filt,
            "$select": "Supplier,SupplierName,CreationDate",
        },
        page_size=odata_page_size_for(SUPPLIER_COLLECTION_PATH),
    )
    ids: set[str] = set()
    for p in props:
        sid = odata_text(p.get("Supplier"))
        if sid:
            ids.add(sid)
    out = sorted(ids)
    log.info("reference_sync vendor delta A_Supplier day=%s: %s supplier id(s)", day, len(out))
    return out


async def fetch_supplier_name_map(
    odata: ODataClient,
    supplier_ids: list[str],
) -> dict[str, dict[str, str]]:
    """``A_Supplier`` → ``{supplier_id: {SupplierName, SupplierFullName}}``."""
    ids = sorted({s.strip() for s in supplier_ids if s and str(s).strip()})
    out: dict[str, dict[str, str]] = {}
    if not ids:
        return out
    batch_size = max(1, VENDOR_NAME_SUPPLIER_BATCH)
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        or_f = supplier_or_filter(batch)
        if not or_f:
            continue
        props = await odata.fetch_properties(
            SUPPLIER_COLLECTION_PATH,
            params={
                "$filter": or_f,
                "$select": "Supplier,SupplierName,SupplierFullName",
            },
            page_size=odata_page_size_for(SUPPLIER_COLLECTION_PATH),
        )
        for p in props:
            sid = odata_text(p.get("Supplier"))
            if not sid:
                continue
            out[sid] = {
                "SupplierName": odata_text(p.get("SupplierName")),
                "SupplierFullName": odata_text(p.get("SupplierFullName")),
            }
    missing = len(ids) - len(out)
    log.info(
        "reference_sync vendor A_Supplier names: asked=%s resolved=%s missing=%s",
        len(ids),
        len(out),
        missing,
    )
    if ids and not out:
        raise RuntimeError(
            f"A_Supplier name enrich resolved 0/{len(ids)} suppliers — "
            "refusing to sync vendors with CoCd labels only"
        )
    if missing and missing > max(5, len(ids) // 10):
        log.warning(
            "reference_sync vendor A_Supplier names: high miss rate missing=%s/%s",
            missing,
            len(ids),
        )
    return out


def apply_supplier_names(
    company_props: list[dict[str, Any]],
    name_map: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Stamp ``SupplierName`` / ``SupplierFullName`` onto ``A_SupplierCompany`` props."""
    for props in company_props:
        sid = str(props.get("Supplier") or "").strip()
        names = name_map.get(sid)
        if not names:
            continue
        if names.get("SupplierName"):
            props["SupplierName"] = names["SupplierName"]
        if names.get("SupplierFullName"):
            props["SupplierFullName"] = names["SupplierFullName"]
    return company_props


async def enrich_supplier_company_with_names(
    odata: ODataClient,
    company_props: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join ``A_Supplier`` display names onto company rows (picker searchable labels)."""
    if not company_props:
        return company_props
    need: list[str] = []
    for p in company_props:
        sid = str(p.get("Supplier") or "").strip()
        if sid and not odata_text(p.get("SupplierName")):
            need.append(sid)
    if not need:
        return company_props
    name_map = await fetch_supplier_name_map(odata, need)
    return apply_supplier_names(company_props, name_map)


async def fetch_vendor_company_for_suppliers(
    odata: ODataClient,
    supplier_ids: list[str],
) -> list[dict[str, Any]]:
    """
    ``A_SupplierCompany`` rows for specific suppliers × workshop company codes.

    Verified on QAS: ``$filter=Supplier eq '1000005974' and CompanyCode eq '1MGH'``
    returns ``PaymentTerms`` (composite catalogue shape).
    """
    if not supplier_ids:
        return []
    company_codes = await vendor_company_codes_for_sync()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _append(entry: dict[str, Any]) -> None:
        props = odata_entity_properties(entry)
        props["_raw_entry"] = entry
        vendor = str(props.get("Supplier") or "").strip()
        cocd = str(props.get("CompanyCode") or "").strip()
        if not vendor or not cocd:
            return
        key = (vendor, cocd)
        if key in seen:
            return
        seen.add(key)
        out.append(props)

    batch_size = max(1, VENDOR_DELTA_SUPPLIER_BATCH)
    for i in range(0, len(supplier_ids), batch_size):
        batch = supplier_ids[i : i + batch_size]
        or_f = supplier_or_filter(batch)
        if not or_f:
            continue
        for cocd in company_codes:
            filt = f"{or_f} and CompanyCode eq {_odata_literal(cocd)}"
            n_before = len(out)
            try:
                async for entry in odata.iter_entities(
                    VENDOR_COLLECTION_PATH,
                    params={
                        "$filter": filt,
                        "$select": "Supplier,CompanyCode,CompanyCodeName,PaymentTerms,AccountingClerk",
                    },
                    page_size=VENDOR_SHARD_PAGE_SIZE,
                    stop_pagination_on_error=True,
                ):
                    _append(entry)
            except Exception:
                log.exception(
                    "reference_sync vendor company fetch failed batch=%s cocd=%s",
                    batch,
                    cocd,
                )
                raise
            added = len(out) - n_before
            if added:
                log.info(
                    "reference_sync vendor company batch %s..%s cocd=%s: +%s (total %s)",
                    batch[0],
                    batch[-1],
                    cocd,
                    added,
                    len(out),
                )
    return await enrich_supplier_company_with_names(odata, out)


async def fetch_vendor_properties_delta(
    odata: ODataClient,
    *,
    day: date,
) -> list[dict[str, Any]]:
    """Daily: new suppliers from ``A_Supplier`` → company rows with ``payt``."""
    supplier_ids = await fetch_delta_supplier_ids(odata, day=day)
    if not supplier_ids:
        return []
    return await fetch_vendor_company_for_suppliers(odata, supplier_ids)


async def fetch_vendor_properties_full(odata: ODataClient) -> list[dict[str, Any]]:
    """
    Full catalogue: ``A_SupplierCompany`` by company shard + coarse unfiltered pass,
    then ``A_Supplier`` names for searchable labels.

    SAP QAS returns HTTP 500 for ``$top=500`` and often for ``$skip=200`` on the full set;
    sharding avoids both issues for workshop orgs.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _append(entry: dict[str, Any]) -> None:
        props = odata_entity_properties(entry)
        props["_raw_entry"] = entry
        vendor = str(props.get("Supplier") or "").strip()
        cocd = str(props.get("CompanyCode") or "").strip()
        if not vendor or not cocd:
            return
        key = (vendor, cocd)
        if key in seen:
            return
        seen.add(key)
        out.append(props)

    company_codes = await vendor_company_codes_for_sync()
    shard_page_size = odata_page_size_for(VENDOR_COLLECTION_PATH)
    for cocd in company_codes:
        n_before = len(out)
        async for entry in odata.iter_entities(
            VENDOR_COLLECTION_PATH,
            params={
                "$filter": f"CompanyCode eq {_odata_literal(cocd)}",
                "$select": _VENDOR_COMPANY_SELECT,
            },
            page_size=shard_page_size,
            stop_pagination_on_error=True,
        ):
            _append(entry)
        log.info(
            "reference_sync vendor shard %s: +%s rows (total %s)",
            cocd,
            len(out) - n_before,
            len(out),
        )

    n_before = len(out)
    async for entry in odata.iter_entities(
        VENDOR_COLLECTION_PATH,
        params={"$select": _VENDOR_COMPANY_SELECT},
        page_size=VENDOR_UNFILTERED_PAGE_SIZE,
        stop_pagination_on_error=True,
    ):
        _append(entry)
    log.info(
        "reference_sync vendor unfiltered pass: +%s rows (total %s)",
        len(out) - n_before,
        len(out),
    )
    return await enrich_supplier_company_with_names(odata, out)


async def upsert_vendors_for_supplier_ids(supplier_ids: list[str]) -> tuple[int, int, int]:
    """Targeted backfill: ``A_SupplierCompany`` for explicit Supplier ids → DB upsert."""
    from app.db.session import AsyncSessionLocal
    from app.procurement.reference_sync.mappers import map_vendor_rows
    from app.procurement.reference_sync.upsert import upsert_reference_rows_batched

    ids = sorted({s.strip() for s in supplier_ids if s and str(s).strip()})
    if not ids:
        return 0, 0, 0
    async with ODataClient.open() as odata:
        props = await fetch_vendor_company_for_suppliers(odata, ids)
    rows = map_vendor_rows(props)
    async with AsyncSessionLocal() as session:
        inserted, updated = await upsert_reference_rows_batched(session, rows)
    return len(rows), inserted, updated


# Back-compat alias used by tests / external imports.
fetch_vendor_properties = fetch_vendor_properties_full
