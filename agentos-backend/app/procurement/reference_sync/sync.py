"""Orchestrate SAP master-data sync into ``pr_po_reference_values``."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from app.db.session import AsyncSessionLocal
from app.infra.row_claim import acquire_claim, refsync_claim_name
from app.procurement.reference_sync import dates as sync_dates
from app.procurement.reference_sync import mappers
from app.procurement.reference_sync.constants import (
    COST_CENTER_COLLECTION_PATH,
    PLANT_COLLECTION_PATH,
    STORAGE_LOCATION_COLLECTION_PATH,
    Z_PURCHASE_REQUISITION_SRV,
)
from app.procurement.reference_sync.material_fetch import fetch_material_props_with_descriptions
from app.procurement.reference_sync.odata_client import (
    ODataClient,
    fetch_collection_properties,
    sap_master_configured,
)
from app.procurement.reference_sync.rows import ReferenceRow
from app.procurement.reference_sync.upsert import (
    prune_reference_domain_rows,
    upsert_reference_rows_batched,
)
from app.procurement.reference_sync.vendor_fetch import (
    fetch_vendor_properties_delta,
    fetch_vendor_properties_full,
)

log = logging.getLogger(__name__)

SyncMode = Literal["daily", "full"]

Z_BASE = Z_PURCHASE_REQUISITION_SRV
PLANT_PATH = PLANT_COLLECTION_PATH
SLOC_PATH = STORAGE_LOCATION_COLLECTION_PATH
CC_PATH = COST_CENTER_COLLECTION_PATH


@dataclass
class DomainSyncResult:
    domain: str
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    pruned: int = 0
    error: str | None = None


@dataclass
class SyncReport:
    mode: SyncMode
    domains: list[DomainSyncResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(d.error is None for d in self.domains)


async def _fetch_and_map(
    *,
    domain: str,
    path: str,
    mapper,
    odata: ODataClient,
    params: dict[str, str] | None = None,
) -> tuple[list[ReferenceRow], int]:
    props = await fetch_collection_properties(
        collection_path=path, params=params, odata=odata
    )
    rows = mapper(props)
    for r in rows:
        if r.domain != domain:
            raise ValueError(f"mapper domain mismatch: {domain!r} vs {r.domain!r}")
    return rows, len(props)


async def _persist_domain_rows(
    *,
    domain: str,
    rows: list[ReferenceRow],
    merge_extra_on_update: bool,
    prune: bool,
) -> tuple[int, int, int]:
    """Upsert ``rows``. Full prune claims the domain for the whole write txn.

    Returns ``(inserted, updated, pruned)``. A busy prune claim skips the write
    (``0, 0, 0``); daily upserts do not claim.
    """
    async with AsyncSessionLocal() as session:
        if prune:
            if not await acquire_claim(session, refsync_claim_name(domain)):
                log.info("reference_sync skipped domain=%s (prune claim held)", domain)
                await session.rollback()
                return 0, 0, 0
            inserted, updated = await upsert_reference_rows_batched(
                session,
                rows,
                merge_extra_on_update=merge_extra_on_update,
                commit=False,
            )
            pruned = 0
            if rows:
                pruned = await prune_reference_domain_rows(
                    session, domain=domain, rows=rows, commit=False
                )
            await session.commit()
            return inserted, updated, pruned
        inserted, updated = await upsert_reference_rows_batched(
            session,
            rows,
            merge_extra_on_update=merge_extra_on_update,
        )
        return inserted, updated, 0


async def _sync_material_domain(
    odata: ODataClient,
    *,
    params: dict[str, str] | None,
    merge_extra: bool,
    prune: bool = False,
) -> DomainSyncResult:
    result = DomainSyncResult(domain="material")
    try:
        log.info("reference_sync starting domain=material")
        props = await fetch_material_props_with_descriptions(odata, params=params)
        rows = mappers.map_material_rows(props)
        result.fetched = len(props)
        result.inserted, result.updated, result.pruned = await _persist_domain_rows(
            domain="material",
            rows=rows,
            merge_extra_on_update=merge_extra,
            prune=prune and result.fetched > 0 and bool(rows),
        )
        log.info(
            "reference_sync material: fetched=%s inserted=%s updated=%s pruned=%s",
            result.fetched,
            result.inserted,
            result.updated,
            result.pruned,
        )
    except Exception as e:
        result.error = str(e)
        log.exception("reference_sync failed domain=material")
    return result


async def _sync_domain(
    *,
    domain: str,
    path: str,
    mapper,
    odata: ODataClient,
    params: dict[str, str] | None,
    merge_extra: bool,
    prune: bool = False,
) -> DomainSyncResult:
    result = DomainSyncResult(domain=domain)
    try:
        log.info("reference_sync starting domain=%s", domain)
        rows, result.fetched = await _fetch_and_map(
            domain=domain,
            path=path,
            mapper=mapper,
            odata=odata,
            params=params,
        )
        result.inserted, result.updated, result.pruned = await _persist_domain_rows(
            domain=domain,
            rows=rows,
            merge_extra_on_update=merge_extra,
            prune=prune and result.fetched > 0 and bool(rows),
        )
        log.info(
            "reference_sync %s: fetched=%s inserted=%s updated=%s pruned=%s",
            domain,
            result.fetched,
            result.inserted,
            result.updated,
            result.pruned,
        )
    except Exception as e:
        result.error = str(e)
        log.exception("reference_sync failed domain=%s", domain)
    return result


async def _sync_vendor_domain(
    odata: ODataClient,
    *,
    mode: SyncMode,
    sync_day: Any | None = None,
    prune: bool = False,
) -> DomainSyncResult:
    result = DomainSyncResult(domain="vendor")
    try:
        log.info("reference_sync starting domain=vendor mode=%s", mode)
        if mode == "full":
            props = await fetch_vendor_properties_full(odata)
        else:
            day = sync_day or sync_dates.sync_yesterday_utc()
            props = await fetch_vendor_properties_delta(odata, day=day)
        rows = mappers.map_vendor_rows(props)
        result.fetched = len(props)
        named = sum(1 for r in rows if r.label and "|" not in r.label)
        log.info(
            "reference_sync vendor mapped=%s with_display_name≈%s",
            len(rows),
            named,
        )
        result.inserted, result.updated, result.pruned = await _persist_domain_rows(
            domain="vendor",
            rows=rows,
            merge_extra_on_update=False,
            prune=prune and mode == "full" and result.fetched > 0 and bool(rows),
        )
        log.info(
            "reference_sync vendor: fetched=%s inserted=%s updated=%s pruned=%s",
            result.fetched,
            result.inserted,
            result.updated,
            result.pruned,
        )
    except Exception as e:
        result.error = str(e)
        log.exception("reference_sync failed domain=vendor")
    return result


async def _sync_matgroup_domains(odata: ODataClient, *, prune: bool = False) -> list[DomainSyncResult]:
    """``MatGroupSet`` → material_group + service_group."""
    out: list[DomainSyncResult] = []
    try:
        props = await fetch_collection_properties(
            collection_path=f"{Z_BASE}/MatGroupSet",
            params={},
            odata=odata,
        )
        mg_rows = mappers.map_matgroup_rows(props)
        sg_rows = mappers.map_service_group_rows(props)
        do_prune = bool(prune)
        ins, upd, pruned_mg = await _persist_domain_rows(
            domain="material_group",
            rows=mg_rows,
            merge_extra_on_update=False,
            prune=do_prune and bool(mg_rows),
        )
        out.append(
            DomainSyncResult(
                domain="material_group",
                fetched=len(props),
                inserted=ins,
                updated=upd,
                pruned=pruned_mg,
            )
        )
        ins2, upd2, pruned_sg = await _persist_domain_rows(
            domain="service_group",
            rows=sg_rows,
            merge_extra_on_update=False,
            prune=do_prune and bool(sg_rows),
        )
        out.append(
            DomainSyncResult(
                domain="service_group",
                fetched=len(props),
                inserted=ins2,
                updated=upd2,
                pruned=pruned_sg,
            )
        )
    except Exception as e:
        msg = str(e)
        log.exception("reference_sync MatGroupSet failed")
        if not out:
            out.append(DomainSyncResult(domain="material_group", error=msg))
            out.append(DomainSyncResult(domain="service_group", error=msg))
        elif len(out) == 1:
            out.append(DomainSyncResult(domain="service_group", error=msg))
    return out


def _full_domain_specs() -> list[tuple[str, str, Any, dict[str, str] | None, bool]]:
    return [
        (
            "plant",
            PLANT_PATH,
            mappers.map_plant_rows,
            {"$select": "Plant,PlantName"},
            False,
        ),
        ("purchasing_group", f"{Z_BASE}/PurGroupSet", mappers.map_pur_group_rows, None, False),
        ("storage_location", SLOC_PATH, mappers.map_storage_location_rows, None, False),
        (
            "cost_center",
            CC_PATH,
            mappers.map_cost_center_rows,
            {
                "$filter": sync_dates.cost_center_full_filter(),
                "$expand": "to_Text",
            },
            True,
        ),
        ("service", f"{Z_BASE}/ServiceSet", mappers.map_service_rows, None, False),
        (
            "asset",
            f"{Z_BASE}/AssetSet",
            mappers.map_asset_rows,
            {"$filter": sync_dates.asset_full_filter()},
            False,
        ),
        ("tax_code", f"{Z_BASE}/TaxCodeSet", mappers.map_tax_code_rows, None, False),
    ]


async def run_reference_sync_full(
    *,
    domains: list[str] | None = None,
    prune: bool = False,
) -> SyncReport:
    report = SyncReport(mode="full")
    if not sap_master_configured():
        report.domains.append(
            DomainSyncResult(domain="*", error="PROCUREMENT_SAP_* not configured")
        )
        return report

    want = set(domains) if domains else None
    async with ODataClient.open() as odata:
        if want is None or "material_group" in want or "service_group" in want:
            report.domains.extend(await _sync_matgroup_domains(odata, prune=prune))

        if want is None or "vendor" in want:
            report.domains.append(await _sync_vendor_domain(odata, mode="full", prune=prune))

        if want is None or "material" in want:
            report.domains.append(
                await _sync_material_domain(
                    odata,
                    params={"$select": "Product,ProductGroup,BaseUnit,ProductType"},
                    merge_extra=False,
                    prune=prune,
                )
            )

        for domain, path, mapper, params, merge_extra in _full_domain_specs():
            if want is not None and domain not in want:
                continue
            report.domains.append(
                await _sync_domain(
                    domain=domain,
                    path=path,
                    mapper=mapper,
                    odata=odata,
                    params=params,
                    merge_extra=merge_extra,
                    prune=prune,
                )
            )
    return report


async def run_reference_sync_daily(
    *,
    day: Any | None = None,
    domains: list[str] | None = None,
) -> SyncReport:
    """
    Daily job: yesterday delta for material, cost_center, vendor, asset;
    full pull for small catalogues (plant, sloc, service, tax, groups).
    """
    report = SyncReport(mode="daily")
    if not sap_master_configured():
        report.domains.append(
            DomainSyncResult(domain="*", error="PROCUREMENT_SAP_* not configured")
        )
        return report

    sync_day = day or sync_dates.sync_yesterday_utc()
    want = set(domains) if domains else None

    def _want(domain: str) -> bool:
        return want is None or domain in want

    async with ODataClient.open() as odata:
        if _want("material_group") or _want("service_group"):
            report.domains.extend(await _sync_matgroup_domains(odata))

        if _want("material"):
            mat_filter = sync_dates.material_daily_filter(sync_day)
            report.domains.append(
                await _sync_material_domain(
                    odata,
                    params={
                        "$filter": mat_filter,
                        "$select": "Product,ProductGroup,BaseUnit,ProductType,CreationDate,LastChangeDate",
                    },
                    merge_extra=False,
                )
            )

        if _want("cost_center"):
            cc_filter = sync_dates.cost_center_daily_filter(sync_day)
            report.domains.append(
                await _sync_domain(
                    domain="cost_center",
                    path=CC_PATH,
                    mapper=mappers.map_cost_center_rows,
                    odata=odata,
                    params={"$filter": cc_filter, "$expand": "to_Text"},
                    merge_extra=True,
                )
            )

        if _want("vendor"):
            report.domains.append(
                await _sync_vendor_domain(odata, mode="daily", sync_day=sync_day)
            )

        if _want("asset"):
            asset_filter = sync_dates.asset_daily_filter(sync_day)
            report.domains.append(
                await _sync_domain(
                    domain="asset",
                    path=f"{Z_BASE}/AssetSet",
                    mapper=mappers.map_asset_rows,
                    odata=odata,
                    params={"$filter": asset_filter},
                    merge_extra=False,
                )
            )

        for domain, path, mapper, params, merge_extra in _full_domain_specs():
            if domain in ("material", "cost_center", "asset"):
                continue
            if not _want(domain):
                continue
            report.domains.append(
                await _sync_domain(
                    domain=domain,
                    path=path,
                    mapper=mapper,
                    odata=odata,
                    params=params,
                    merge_extra=merge_extra,
                )
            )
    return report


async def run_reference_sync(*, mode: SyncMode = "daily") -> SyncReport:
    if mode == "full":
        return await run_reference_sync_full()
    return await run_reference_sync_daily()
