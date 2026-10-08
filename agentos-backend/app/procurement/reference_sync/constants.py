"""Reference sync tuning — constants only (no extra env vars)."""

from __future__ import annotations

# OData collection paths (shared by sync + pagination policy).
Z_PURCHASE_REQUISITION_SRV = "/sap/opu/odata/sap/Z_PURCHASE_REQUISITION_SRV"
PLANT_COLLECTION_PATH = "/sap/opu/odata/sap/API_PLANT_SRV/A_Plant"
STORAGE_LOCATION_COLLECTION_PATH = (
    "/sap/opu/odata/sap/API_STORAGELOCATION_SRV/StorageLocation"
)
COST_CENTER_COLLECTION_PATH = "/sap/opu/odata/sap/API_COSTCENTER_SRV/A_CostCenter"
PRODUCT_COLLECTION_PATH = "/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product"
PRODUCT_DESCRIPTION_COLLECTION_PATH = (
    "/sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductDescription"
)

# OData pagination ($skip/$top when __next is absent on this gateway).
ODATA_PAGE_SIZE = 500
# Per-collection overrides (SAP gateway limits differ by entity set).
ODATA_PAGE_SIZE_BY_COLLECTION: dict[str, int] = {
    # QAS: $top >= 250 on A_SupplierCompany → HTTP 500 (internal server error).
    "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_SupplierCompany": 200,
}

# Stable $orderby for paged catalogue reads (avoids duplicate/missing rows when $skip advances).
ODATA_ORDERBY_BY_COLLECTION: dict[str, str] = {
    PRODUCT_COLLECTION_PATH: "Product",
    PRODUCT_DESCRIPTION_COLLECTION_PATH: "Product",
    PLANT_COLLECTION_PATH: "Plant",
    STORAGE_LOCATION_COLLECTION_PATH: "Plant,StorageLocation",
    COST_CENTER_COLLECTION_PATH: "CostCenter",
    f"{Z_PURCHASE_REQUISITION_SRV}/MatGroupSet": "MATKL",
    f"{Z_PURCHASE_REQUISITION_SRV}/PurGroupSet": "EKGRP",
    f"{Z_PURCHASE_REQUISITION_SRV}/ServiceSet": "Asnum",
    f"{Z_PURCHASE_REQUISITION_SRV}/AssetSet": "Bukrs,Anln1",
    f"{Z_PURCHASE_REQUISITION_SRV}/TaxCodeSet": "Mwskz",
    "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_Supplier": "Supplier",
    "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_SupplierCompany": "Supplier",
}


def odata_page_size_for(collection_path: str) -> int:
    return ODATA_PAGE_SIZE_BY_COLLECTION.get(collection_path, ODATA_PAGE_SIZE)


def odata_collection_params(
    collection_path: str,
    params: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge default ``$orderby`` for *collection_path* when caller did not set one."""
    out = dict(params or {})
    if "$orderby" not in out:
        orderby = ODATA_ORDERBY_BY_COLLECTION.get(collection_path)
        if orderby:
            out["$orderby"] = orderby
    return out


# Vendor sync — see vendor_fetch.py.
SUPPLIER_COLLECTION_PATH = "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_Supplier"
VENDOR_COLLECTION_PATH = "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_SupplierCompany"
# Max suppliers per ``$filter`` OR-clause when resolving company rows (URL length).
VENDOR_DELTA_SUPPLIER_BATCH = 8
# Name-only ``A_Supplier`` lookups are lighter — larger OR batches cut full-sync wall time.
VENDOR_NAME_SUPPLIER_BATCH = 40
VENDOR_SHARD_PAGE_SIZE = 50
VENDOR_UNFILTERED_PAGE_SIZE = 200

# Safety cap — prevents runaway loops if SAP ignores $skip (≈2.5M rows max).
ODATA_MAX_PAGES = 10_000

# GET retries for transient SAP / network errors.
HTTP_GET_MAX_ATTEMPTS = 3
HTTP_GET_RETRY_BACKOFF_SEC = (2.0, 5.0, 10.0)
HTTP_GET_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# DB upsert batch size (per commit). Bulk ON CONFLICT makes larger batches safe.
UPSERT_COMMIT_EVERY = 2000

# APScheduler cron (UTC) — same idea as other kernel jobs with fixed times.
SYNC_CRON_HOUR = 2
SYNC_CRON_MINUTE = 30
