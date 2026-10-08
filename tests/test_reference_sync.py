"""Unit tests for SAP reference sync mappers and upsert helpers."""

from __future__ import annotations

import pytest

from datetime import date

from app.procurement.reference_sync.dates import (
    asset_daily_filter,
    asset_full_filter,
    cost_center_daily_filter,
    cost_center_full_filter,
    material_daily_filter,
    sync_yesterday_utc,
    vendor_daily_filter,
    vendor_full_filter,
)
from app.procurement.reference_sync.vendor_fetch import apply_supplier_names, supplier_or_filter
from app.procurement.reference_sync.upsert import _reference_row_keys
from app.procurement.reference_sync.material_fetch import attach_product_descriptions
from app.procurement.reference_sync.mappers import (
    map_asset_rows,
    map_matgroup_rows,
    map_material_rows,
    map_storage_location_rows,
    map_supplier_rows,
    map_vendor_rows,
)
from app.procurement.reference_sync.constants import (
    COST_CENTER_COLLECTION_PATH,
    PRODUCT_COLLECTION_PATH,
    odata_collection_params,
    odata_page_size_for,
)
from app.procurement.reference_sync.odata_client import (
    _should_retry,
    _skip_pagination_should_stop,
)
from app.procurement.reference_sync.rows import ReferenceRow, dedupe_reference_rows, merge_extra

import httpx


def test_dedupe_reference_rows_last_wins() -> None:
    a = ReferenceRow(domain="tax_code", code="N3", label="first")
    b = ReferenceRow(domain="tax_code", code="N3", label="second")
    out = dedupe_reference_rows([a, b])
    assert len(out) == 1
    assert out[0].label == "second"


def test_merge_extra_preserves_hana_facets() -> None:
    existing = {"Entity": "1MGH", "Department": "HR", "Profit Center": "CO99"}
    incoming = {"Entity": "1MGH", "Department": ""}
    out = merge_extra(existing, incoming)
    assert out is not None
    assert out["Department"] == "HR"


def test_map_matgroup() -> None:
    rows = map_matgroup_rows([{"MATKL": "S001-0001", "WGBEZ": "Test group"}])
    assert len(rows) == 1
    assert rows[0].domain == "material_group"
    assert rows[0].code == "S001-0001"
    assert rows[0].label == "Test group"


def test_map_material_rows_uses_product_description() -> None:
    props = [
        {
            "Product": "4000000069",
            "ProductGroup": "M019-0001",
            "BaseUnit": "PC",
            "ProductType": "YUNB",
            "ProductDescription": 'BIODEGRADABLE PLATE 6" 2000/PACK',
        }
    ]
    rows = map_material_rows(props)
    assert len(rows) == 1
    assert rows[0].label == 'BIODEGRADABLE PLATE 6" 2000/PACK'
    assert rows[0].extra is not None
    assert rows[0].extra["description"] == 'BIODEGRADABLE PLATE 6" 2000/PACK'
    assert rows[0].extra["material_type"] == "YUNB"


def test_attach_product_descriptions_sets_field() -> None:
    props = [{"Product": "4000000002", "ProductType": "YUNB"}]
    attach_product_descriptions(props, {"4000000002": "A4 COPIER PAPER 75 GSM"})
    assert props[0]["ProductDescription"] == "A4 COPIER PAPER 75 GSM"
    rows = map_material_rows(props)
    assert rows[0].label == "A4 COPIER PAPER 75 GSM"


def test_map_asset_rows_composite_code() -> None:
    rows = map_asset_rows(
        [
            {
                "Anln1": "003500008160",
                "Bukrs": "1MGH",
                "Anlkl": "00003005",
                "Txt50": "RACK-SR-02",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0].domain == "asset"
    assert rows[0].code == "1MGH|003500008160"
    assert rows[0].label == "RACK-SR-02"
    assert rows[0].extra is not None
    assert rows[0].extra["company_code"] == "1MGH"
    assert rows[0].extra["asset_number"] == "003500008160"


def test_map_storage_composite_code() -> None:
    rows = map_storage_location_rows(
        [{"Plant": "H001", "StorageLocation": "3021", "StorageLocationName": "Main"}]
    )
    assert rows[0].code == "H001|3021"
    assert rows[0].extra == {"plant": "H001", "storage_location": "3021"}


def test_map_vendor_composite() -> None:
    rows = map_vendor_rows(
        [
            {
                "Supplier": "8002",
                "CompanyCode": "1MGH",
                "CompanyCodeName": "TATA 1MG Health S Pvt Ltd",
                "SupplierName": "AD ENTERPRISES",
                "SupplierFullName": " AD ENTERPRISES/201301 Noida",
                "PaymentTerms": "YI08",
            }
        ]
    )
    assert rows[0].code == "8002|1MGH"
    assert rows[0].label == "AD ENTERPRISES"
    assert rows[0].extra is not None
    assert rows[0].extra.get("name_1") == "AD ENTERPRISES"
    assert rows[0].extra.get("name_2") == "AD ENTERPRISES/201301 Noida"
    assert rows[0].extra.get("company_code_name") == "TATA 1MG Health S Pvt Ltd"
    assert rows[0].extra.get("payt") == "YI08"


def test_map_vendor_falls_back_without_supplier_name() -> None:
    rows = map_vendor_rows(
        [{"Supplier": "8002", "CompanyCode": "1MGH", "CompanyCodeName": "Acme India"}]
    )
    assert rows[0].code == "8002|1MGH"
    # No SupplierName → label falls back to vendor|cocd (not CoCd legal name).
    assert rows[0].label == "8002|1MGH"
    assert rows[0].extra is not None
    assert rows[0].extra.get("name_1") == "Acme India"


def test_apply_supplier_names_stamps_company_rows() -> None:
    props = [{"Supplier": "8002", "CompanyCode": "1MGH", "CompanyCodeName": "CoCd"}]
    apply_supplier_names(
        props,
        {"8002": {"SupplierName": "AD ENTERPRISES", "SupplierFullName": "AD ENT / Noida"}},
    )
    assert props[0]["SupplierName"] == "AD ENTERPRISES"
    rows = map_vendor_rows(props)
    assert rows[0].label == "AD ENTERPRISES"


def test_reference_row_keys_skip_empty_code() -> None:
    keys = _reference_row_keys(
        [
            ReferenceRow(domain="vendor", code="1|1MGH", label="a"),
            ReferenceRow(domain="vendor", code="", label="b"),
            ReferenceRow(domain="vendor", code="1|1MGH", label="dup"),
        ]
    )
    assert keys == [("", "1|1MGH", "")]


@pytest.mark.asyncio
async def test_prune_refuses_empty_wipe() -> None:
    """Empty SAP key set must not delete the whole domain."""
    from types import SimpleNamespace
    from app.procurement.reference_sync.upsert import prune_reference_domain_rows

    class _Sess:
        def __init__(self) -> None:
            self.executed = 0

        async def execute(self, *_a, **_k):
            self.executed += 1
            return SimpleNamespace(rowcount=99)

        async def commit(self) -> None:
            return None

    sess = _Sess()
    deleted = await prune_reference_domain_rows(sess, domain="vendor", rows=[])  # type: ignore[arg-type]
    assert deleted == 0
    assert sess.executed == 0


def test_vendor_collection_page_size_cap() -> None:
    assert (
        odata_page_size_for(
            "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_SupplierCompany"
        )
        == 200
    )
    assert odata_page_size_for("/sap/opu/odata/sap/API_PLANT_SRV/A_Plant") == 500


def test_odata_collection_params_adds_default_orderby() -> None:
    out = odata_collection_params(PRODUCT_COLLECTION_PATH, {"$select": "Product"})
    assert out["$orderby"] == "Product"
    assert out["$select"] == "Product"


def test_odata_collection_params_respects_caller_orderby() -> None:
    out = odata_collection_params(
        COST_CENTER_COLLECTION_PATH,
        {"$orderby": "ValidityStartDate"},
    )
    assert out["$orderby"] == "ValidityStartDate"


def test_skip_pagination_stops_on_oversized_page() -> None:
    """SAP may ignore $top and return the full catalogue (TaxCodeSet)."""
    assert (
        _skip_pagination_should_stop(
            len_rows=1248,
            page_size=500,
            skip=0,
            prev_fingerprint=None,
            cur_fingerprint="x",
            has_next=False,
        )
        is True
    )


def test_skip_pagination_stops_on_repeated_page() -> None:
    assert (
        _skip_pagination_should_stop(
            len_rows=500,
            page_size=500,
            skip=500,
            prev_fingerprint="same",
            cur_fingerprint="same",
            has_next=False,
        )
        is True
    )


def test_skip_pagination_continues_on_full_page() -> None:
    assert (
        _skip_pagination_should_stop(
            len_rows=500,
            page_size=500,
            skip=0,
            prev_fingerprint=None,
            cur_fingerprint="page1",
            has_next=False,
        )
        is False
    )


def test_odata_should_retry_transient() -> None:
    assert _should_retry(httpx.TimeoutException("t"), None) is True
    assert _should_retry(httpx.ConnectError("c"), None) is True
    resp = httpx.Response(503, request=httpx.Request("GET", "http://x"))
    assert (
        _should_retry(
            httpx.HTTPStatusError("s", request=resp.request, response=resp),
            None,
        )
        is True
    )
    resp400 = httpx.Response(400, request=httpx.Request("GET", "http://x"))
    assert (
        _should_retry(
            httpx.HTTPStatusError("b", request=resp400.request, response=resp400),
            None,
        )
        is False
    )


def test_daily_filters_contain_yesterday() -> None:
    day = sync_yesterday_utc()
    mf = material_daily_filter(day)
    assert "CreationDate" in mf and "LastChangeDate" in mf
    cf = cost_center_daily_filter(day)
    assert "ValidityStartDate" in cf


def test_asset_full_filter_created_on_range() -> None:
    af = asset_full_filter(end=date(2026, 6, 10))
    assert af == "CreatedOn ge '20210101' and CreatedOn le '20260610'"


def test_asset_daily_filter_yesterday() -> None:
    af = asset_daily_filter(date(2026, 6, 9))
    assert "CreatedOn ge '20260609'" in af
    assert "ChangedOn ge '20260609'" in af


def test_map_supplier_rows() -> None:
    rows = map_supplier_rows(
        [{"Supplier": "8003", "SupplierName": "Acme", "CreationDate": "/Date(1717113600000)/"}]
    )
    assert rows[0].code == "8003"
    assert rows[0].label == "Acme"


def test_supplier_or_filter_batches() -> None:
    assert supplier_or_filter(["8001"]) == "Supplier eq '8001'"
    assert supplier_or_filter(["8001", "8002"]) == "(Supplier eq '8001' or Supplier eq '8002')"
    assert "''" in supplier_or_filter(["O'Brien"])


def test_vendor_full_filter_from_2021() -> None:
    vf = vendor_full_filter(end=date(2026, 6, 10))
    assert "CreationDate ge datetime'2021-01-01T00:00:00'" in vf
    assert "CreationDate le datetime'2026-06-10T23:59:59'" in vf


def test_cost_center_full_filter_validity_start() -> None:
    cf = cost_center_full_filter(end=date(2026, 6, 10))
    assert "ValidityStartDate ge datetime'2021-01-01T00:00:00'" in cf
    assert "ValidityStartDate le datetime'2026-06-10T23:59:59'" in cf
