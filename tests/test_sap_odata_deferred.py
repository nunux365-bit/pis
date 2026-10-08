"""OData deferred navigation helpers for SAP PR reads."""

from __future__ import annotations

import pytest

from app.procurement.sap_odata_deferred import (
    PO_SCHEDULE_NAV,
    append_odata_expand,
    po_read_items_need_resolution,
    pr_read_items_need_resolution,
)
from app.procurement.sap_ticket_form_read import form_from_po_sap_read
from app.procurement.sap_odata_utils import odata_deferred_uri, odata_results_list
from app.procurement.sap_ticket_form_read import form_from_pr_sap_read, merge_pr_form_from_seed


def test_odata_results_list_ignores_deferred_stub() -> None:
    node = {"__deferred": {"uri": "https://example/items"}}
    assert odata_results_list(node) == []
    assert odata_deferred_uri(node) == "https://example/items"


def test_append_odata_expand() -> None:
    base = "https://host/Header('1')/to_Items"
    assert append_odata_expand(base, "to_PurchaseReqnAcctAssgmt") == (
        "https://host/Header('1')/to_Items?$expand=to_PurchaseReqnAcctAssgmt"
    )


def test_po_read_items_need_resolution() -> None:
    deferred = {"__deferred": {"uri": "https://x/po-items"}}
    assert po_read_items_need_resolution(deferred) is True
    inline = {"results": [{"PurchaseOrderItem": "10", "Material": "4100000012"}]}
    assert po_read_items_need_resolution(inline) is False


def test_form_from_po_sap_read_schedule_delivery() -> None:
    body = {
        "d": {
            "Supplier": "1000000002",
            "PurchasingOrganization": "1LFS",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4100000012",
                        "NetPriceAmount": "100.00",
                        "PurchaseOrderQuantityUnit": "PC",
                        PO_SCHEDULE_NAV: {
                            "results": [
                                {
                                    "ScheduleLineDeliveryDate": "/Date(1786752000000)/",
                                    "ScheduleLineOrderQuantity": "5",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    form = form_from_po_sap_read(body, document_type="YUNB")
    assert len(form["lines"]) == 1
    assert form["lines"][0]["delivery_date"] == "2026-08-15"
    assert form["lines"][0]["material"] == "4100000012"


@pytest.mark.asyncio
async def test_resolve_po_read_body_follows_deferred_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items_uri = "https://host/A_PurchaseOrder('4080000012')/to_PurchaseOrderItem"
    sched_uri = "https://host/schedule"
    body_in = {
        "d": {
            "PurchaseOrder": "4080000012",
            "to_PurchaseOrderItem": {"__deferred": {"uri": items_uri}},
        }
    }
    items_payload = {
        "d": {
            "results": [
                {
                    "PurchaseOrder": "4080000012",
                    "PurchaseOrderItem": "10",
                    "Material": "4100000012",
                    PO_SCHEDULE_NAV: {"__deferred": {"uri": sched_uri}},
                }
            ]
        }
    }
    sched_payload = {
        "d": {
            "results": [
                {
                    "ScheduleLineDeliveryDate": "/Date(1786752000000)/",
                    "ScheduleLineOrderQuantity": "5",
                }
            ]
        }
    }
    calls: list[str] = []

    async def fake_get(
        client: object,
        *,
        base: str,
        ticket_id: str,
        url: str,
        csrf_service_root: str | None = None,
    ) -> dict[str, object]:
        calls.append(url)
        if url.startswith(items_uri):
            return items_payload
        if url == sched_uri:
            return sched_payload
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(
        "app.procurement.sap_odata_deferred._odata_get_json",
        fake_get,
    )
    from app.procurement.sap_odata_deferred import resolve_po_read_body

    out = await resolve_po_read_body(
        object(),
        base="https://host",
        body=body_in,
        po_number="4080000012",
        ticket_id="t-po",
    )
    items = out["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    assert items[0]["Material"] == "4100000012"
    sched = items[0][PO_SCHEDULE_NAV]["results"]
    assert sched[0]["ScheduleLineDeliveryDate"] == "/Date(1786752000000)/"
    assert append_odata_expand(items_uri, PO_SCHEDULE_NAV) in calls[0]


@pytest.mark.asyncio
async def test_resolve_po_read_body_retries_deferred_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.procurement.sap_pr_client import _SapRequestError
    from app.procurement.sap_odata_deferred import resolve_po_read_body

    sched_uri = "https://host/schedule"
    body_in = {
        "d": {
            "PurchaseOrder": "4080000012",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrder": "4080000012",
                        "PurchaseOrderItem": "10",
                        "Material": "4100000012",
                        PO_SCHEDULE_NAV: {"__deferred": {"uri": sched_uri}},
                    }
                ]
            },
        }
    }
    sched_payload = {
        "d": {
            "results": [
                {
                    "ScheduleLineDeliveryDate": "/Date(1786752000000)/",
                    "ScheduleLineOrderQuantity": "5",
                }
            ]
        }
    }
    calls = 0

    async def fake_get(
        client: object,
        *,
        base: str,
        ticket_id: str,
        url: str,
        csrf_service_root: str | None = None,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if url == sched_uri:
            if calls < 2:
                raise _SapRequestError(
                    "SAP HTTP 404: Resource not found for the segment 'to_ScheduleLine'."
                )
            return sched_payload
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(
        "app.procurement.sap_odata_deferred._odata_get_json",
        fake_get,
    )

    out = await resolve_po_read_body(
        object(),
        base="https://host",
        body=body_in,
        po_number="4080000012",
        ticket_id="t-po",
    )
    items = out["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    assert items[0][PO_SCHEDULE_NAV]["results"][0]["ScheduleLineDeliveryDate"] == "/Date(1786752000000)/"
    assert calls >= 2


@pytest.mark.asyncio
async def test_resolve_po_read_body_retries_mapping_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.procurement.sap_pr_client import _SapRequestError
    from app.procurement.sap_odata_deferred import resolve_po_read_body

    sched_uri = "https://host/schedule"
    body_in = {
        "d": {
            "PurchaseOrder": "4080000012",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrder": "4080000012",
                        "PurchaseOrderItem": "10",
                        "Material": "4100000012",
                        PO_SCHEDULE_NAV: {"__deferred": {"uri": sched_uri}},
                    }
                ]
            },
        }
    }
    sched_payload = {
        "d": {
            "results": [
                {
                    "ScheduleLineDeliveryDate": "/Date(1786752000000)/",
                    "ScheduleLineOrderQuantity": "5",
                }
            ]
        }
    }
    calls = 0

    async def fake_get(
        client: object,
        *,
        base: str,
        ticket_id: str,
        url: str,
        csrf_service_root: str | None = None,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if url == sched_uri:
            if calls < 2:
                raise _SapRequestError(
                    "SAP HTTP 500: Invalid or no mapping to system data types found"
                )
            return sched_payload
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(
        "app.procurement.sap_odata_deferred._odata_get_json",
        fake_get,
    )

    out = await resolve_po_read_body(
        object(),
        base="https://host",
        body=body_in,
        po_number="4080000012",
        ticket_id="t-po",
    )
    items = out["d"]["to_PurchaseOrderItem"]["results"]
    assert items[0][PO_SCHEDULE_NAV]["results"][0]["ScheduleLineDeliveryDate"] == "/Date(1786752000000)/"
    assert calls >= 2


@pytest.mark.asyncio
async def test_resolve_po_read_body_skips_deferred_when_schedule_inlined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.procurement.sap_odata_deferred import resolve_po_read_body

    body_in = {
        "d": {
            "PurchaseOrder": "4080000012",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrder": "4080000012",
                        "PurchaseOrderItem": "10",
                        "Material": "4100000012",
                        PO_SCHEDULE_NAV: {
                            "results": [
                                {
                                    "ScheduleLineDeliveryDate": "/Date(1786752000000)/",
                                    "ScheduleLineOrderQuantity": "5",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }

    async def fail_get(*_a: object, **_k: object) -> dict[str, object]:
        raise AssertionError("deferred GET should not run when schedule is inlined")

    monkeypatch.setattr(
        "app.procurement.sap_odata_deferred._odata_get_json",
        fail_get,
    )

    out = await resolve_po_read_body(
        object(),
        base="https://host",
        body=body_in,
        po_number="4080000012",
        ticket_id="t-po",
    )
    sched = out["d"]["to_PurchaseOrderItem"]["results"][0][PO_SCHEDULE_NAV]["results"]
    assert sched[0]["ScheduleLineDeliveryDate"] == "/Date(1786752000000)/"


@pytest.mark.asyncio
async def test_resolve_po_read_body_fetches_items_when_header_nav_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.procurement.sap_odata_deferred import resolve_po_read_body

    body_in = {"d": {"PurchaseOrder": "4080005769"}}

    async def fake_get(
        client: object,
        *,
        base: str,
        ticket_id: str,
        url: str,
        csrf_service_root: str | None = None,
    ) -> dict[str, object]:
        assert "/to_PurchaseOrderItem" in url
        return {
            "d": {
                "results": [
                    {
                        "PurchaseOrder": "4080005769",
                        "PurchaseOrderItem": "10",
                        PO_SCHEDULE_NAV: {
                            "results": [
                                {"ScheduleLineDeliveryDate": "/Date(1784160000000)/"}
                            ]
                        },
                    }
                ]
            }
        }

    monkeypatch.setattr(
        "app.procurement.sap_odata_deferred._odata_get_json",
        fake_get,
    )

    out = await resolve_po_read_body(
        object(),
        base="https://host",
        body=body_in,
        po_number="4080005769",
        ticket_id="t-po-missing-nav",
    )
    items = out["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    assert items[0]["PurchaseOrderItem"] == "10"


@pytest.mark.asyncio
async def test_resolve_pr_read_body_fetches_items_when_header_nav_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.procurement.sap_odata_deferred import resolve_pr_read_body

    body_in = {"d": {"PurchaseRequisition": "1040005825"}}

    async def fake_get(
        client: object,
        *,
        base: str,
        ticket_id: str,
        url: str,
        csrf_service_root: str | None = None,
    ) -> dict[str, object]:
        assert "/to_PurchaseReqnItem" in url
        return {
            "d": {
                "results": [
                    {
                        "PurchaseRequisition": "1040005825",
                        "PurchaseRequisitionItem": "10",
                        "to_PurchaseReqnAcctAssgmt": {"results": [{"CostCenter": "1000"}]},
                    }
                ]
            }
        }

    monkeypatch.setattr(
        "app.procurement.sap_odata_deferred._odata_get_json",
        fake_get,
    )

    out = await resolve_pr_read_body(
        object(),
        base="https://host",
        body=body_in,
        pr_number="1040005825",
        ticket_id="t-pr-missing-nav",
    )
    items = out["d"]["to_PurchaseReqnItem"]["results"]
    assert len(items) == 1
    assert items[0]["PurchaseRequisitionItem"] == "10"


@pytest.mark.asyncio
async def test_odata_get_json_with_retry_does_not_retry_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.procurement.sap_pr_client import _SapRequestError
    from app.procurement.sap_odata_deferred import _odata_get_json_with_retry

    calls = 0

    async def fake_get(
        client: object,
        *,
        base: str,
        ticket_id: str,
        url: str,
        csrf_service_root: str | None = None,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise _SapRequestError("Unauthorized — check SAP credentials")

    monkeypatch.setattr(
        "app.procurement.sap_odata_deferred._odata_get_json",
        fake_get,
    )

    with pytest.raises(_SapRequestError):
        await _odata_get_json_with_retry(
            object(),
            base="https://host",
            ticket_id="t",
            url="https://host/x",
        )
    assert calls == 1


def test_pr_read_items_need_resolution() -> None:
    deferred = {"__deferred": {"uri": "https://x/items"}}
    assert pr_read_items_need_resolution(deferred) is True
    inline = {
        "results": [{"PurchaseRequisitionItem": "10", "PurchaseRequisitionItemText": "A"}]
    }
    assert pr_read_items_need_resolution(inline) is False


def test_merge_pr_form_from_seed_skips_yast_asset_seed_cc() -> None:
    sap_form = {
        "header": {},
        "lines": [
            {
                "material": "4000000002",
                "asset": "003500008160",
                "purchase_requisition_item": "10",
                "allocations": [{"asset": "003500008160", "qty": "1.000"}],
            }
        ],
    }
    seed = {
        "lines": [
            {
                "material": "4000000002",
                "asset": "003500008160",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "HBM11001A0", "qty": "1"}],
            }
        ],
    }
    merged = merge_pr_form_from_seed(sap_form, document_type="YAST", seed_form=seed)
    assert merged["lines"][0]["allocations"] == [{"asset": "003500008160", "qty": "1.000"}]


def test_merge_pr_form_from_seed_fills_yser_service() -> None:
    sap_form = {
        "header": {"plant": "H002"},
        "lines": [
            {
                "service": "",
                "short_text": "From SAP",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "", "qty": "1"}],
            }
        ],
    }
    seed = {
        "header": {"header_note": "DB note"},
        "lines": [
            {
                "service": "10000000006",
                "short_text": "DB line",
                "purchase_requisition_item": "10",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            }
        ],
    }
    merged = merge_pr_form_from_seed(sap_form, document_type="YSER", seed_form=seed)
    assert merged["lines"][0]["service"] == "10000000006"
    assert merged["lines"][0]["allocations"][0]["cost_center"] == "HCO91001H0"
    assert merged["header"]["header_note"] == "DB note"


def test_merge_pr_form_from_seed_preserves_per_line_tax_codes() -> None:
    """SAP YSER PR GET omits TaxCode; DB seed must restore mixed line taxes on hydrate."""
    from app.procurement.field_schema import normalize_form

    sap_form = {
        "header": {"tax_code": "FA"},
        "lines": [
            {
                "service": "000000001000000061",
                "service_group": "S082-0001",
                "purchase_requisition_item": "10",
                "sap_pr_service_ref": "REF-001",
            },
            {
                "service": "000000001000000219",
                "service_group": "S082-0001",
                "purchase_requisition_item": "10",
                "sap_pr_service_ref": "REF-002",
            },
            {
                "service": "000000001000000226",
                "service_group": "S074-0001",
                "purchase_requisition_item": "20",
                "sap_pr_service_ref": "REF-003",
            },
            {
                "service": "000000001000000389",
                "service_group": "S074-0001",
                "purchase_requisition_item": "20",
                "sap_pr_service_ref": "REF-004",
            },
        ],
    }
    seed = {
        "header": {"tax_code": "FA"},
        "lines": [
            {
                "service": "000000001000000061",
                "purchase_requisition_item": "10",
                "sap_pr_service_ref": "REF-001",
                "tax_code": "FA",
            },
            {
                "service": "000000001000000219",
                "purchase_requisition_item": "10",
                "sap_pr_service_ref": "REF-002",
                "tax_code": "FA",
            },
            {
                "service": "000000001000000226",
                "purchase_requisition_item": "20",
                "sap_pr_service_ref": "REF-003",
                "tax_code": "HA",
            },
            {
                "service": "000000001000000389",
                "purchase_requisition_item": "20",
                "sap_pr_service_ref": "REF-004",
                "tax_code": "HA",
            },
        ],
    }
    merged = merge_pr_form_from_seed(sap_form, document_type="YSER", seed_form=seed)
    out = normalize_form("YSER", merged)
    assert [ln.get("tax_code") for ln in out["lines"]] == ["FA", "FA", "HA", "HA"]


def test_merge_pr_form_from_seed_tax_matches_item_and_service_without_ref() -> None:
    sap_form = {
        "header": {},
        "lines": [
            {
                "service": "000000001000000061",
                "purchase_requisition_item": "10",
            },
            {
                "service": "000000001000000226",
                "purchase_requisition_item": "20",
            },
        ],
    }
    seed = {
        "header": {"tax_code": "FA"},
        "lines": [
            {
                "service": "000000001000000061",
                "purchase_requisition_item": "10",
                "tax_code": "FA",
            },
            {
                "service": "000000001000000226",
                "purchase_requisition_item": "20",
                "tax_code": "HA",
            },
        ],
    }
    merged = merge_pr_form_from_seed(sap_form, document_type="YSER", seed_form=seed)
    assert merged["lines"][0]["tax_code"] == "FA"
    assert merged["lines"][1]["tax_code"] == "HA"


@pytest.mark.asyncio
async def test_get_yser_pr_resolves_z_body(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = {
        "d": {
            "PRNumber": "1010000999",
            "to_Items": {
                "results": [
                    {
                        "PRItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000010000000006",
                                    "ShortText": "Svc",
                                    "CostCenter": "HCO91001H0",
                                    "DistrQuantity": "1.000",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }

    async def fake_z_get_body(client, *, base, pr_number, ticket_id):
        return resolved, None

    monkeypatch.setattr("app.procurement.sap_pr_z_client._z_get_pr_body", fake_z_get_body)
    from app.procurement.sap_pr_z_client import get_yser_pr
    from app.procurement.sap_pr_z_payload import form_from_z_pr_read

    body, err = await get_yser_pr(pr_number="1010000999", ticket_id="t-read")
    assert err is None
    form = form_from_z_pr_read(body or {}, document_type="YSER")
    assert len(form["lines"]) == 1
    assert form["lines"][0]["service"] == "000000010000000006"


def test_form_from_pr_sap_read_with_inline_items() -> None:
    body = {
        "d": {
            "PurReqnDescription": "Note",
            "to_PurchaseReqnItem": {
                "results": [
                    {
                        "PurchaseRequisitionItem": "10",
                        "Material": "4100000012",
                        "PurchaseRequisitionItemText": "Line",
                        "PurchaseRequisitionPrice": "5.00",
                        "to_PurchaseReqnAcctAssgmt": {
                            "results": [
                                {"CostCenter": "HCO91001H0", "Quantity": "1.000"}
                            ]
                        },
                    }
                ]
            },
        }
    }
    form = form_from_pr_sap_read(body, document_type="YUNB")
    assert len(form["lines"]) == 1
    assert form["lines"][0]["allocations"][0]["cost_center"] == "HCO91001H0"
