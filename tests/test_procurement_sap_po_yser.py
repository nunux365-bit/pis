"""YSER PO payload mapping — ZAPI_PURCHASEORDER_PROCESS_SRV."""

from __future__ import annotations

import pytest

from app.procurement.sap_po_z_payload import (
    PO_SERVICES_NAV,
    PO_SERVICE_ACCT_NAV,
    SapZPoItemSnapshot,
    SapZPoServiceSnapshot,
    build_z_po_texts_post_body,
    build_z_yser_po_inner_payload,
    build_z_yser_po_post_body,
    build_z_yser_po_resubmit_plan,
    form_from_z_po_read,
    normalize_service_performer_code,
    po_texts_differ,
    verify_po_texts_against_form,
)
from app.procurement.sap_pr_payload import SapAcctSegment
from tests.conftest_procurement_sap import sample_yser_po_form

_YSER_SAMPLE_DELIVERY = "2026-08-15"
_YSER_SAMPLE_SERVICE_GROUP = "S001-0001"


def _yser_item_snap(**overrides: object) -> SapZPoItemSnapshot:
    base: dict[str, object] = {
        "item_number": "10",
        "is_deleted": False,
        "item_text": "Service PO line",
        "net_price_amount": "200.000",
        "account_assignment_cat": "K",
        "delivery_date": _YSER_SAMPLE_DELIVERY,
        "material_group": _YSER_SAMPLE_SERVICE_GROUP,
        "tax_code": "XE",
        "services": [
            SapZPoServiceSnapshot(
                service=normalize_service_performer_code("10000000006"),
                confirmed_quantity="2.000",
                net_price_amount="200.000",
                net_amount="100.000",
                quantity_unit="EA",
                acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
            )
        ],
    }
    base.update(overrides)
    return SapZPoItemSnapshot(**base)  # type: ignore[arg-type]


def test_build_z_yser_po_create_sets_ext_source_system() -> None:
    form = sample_yser_po_form()
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="57585761-bb41-4369-83fe-49be9ceaf565")
    assert inner["Extsourcesystem"] == "AO57585761BB41436983FE49BE9CEAF565"
    row = inner["to_PurchaseOrderItem"][0]
    assert "AO" not in row.get("PurchaseOrderItemText", "")


def test_build_z_yser_po_standalone_shape() -> None:
    form = sample_yser_po_form()
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    assert "PaymentTerms" not in inner
    assert inner["PurchaseOrderType"] == "YSER"
    items = inner["to_PurchaseOrderItem"]
    assert len(items) == 1
    row = items[0]
    assert row["PurchaseOrderItemCategory"] == "9"
    assert row["PurchaseOrderItem"] == "00010"
    assert row["OrderQuantity"] == "0.000"
    assert row["PurchaseOrderQuantityUnit"] == "EA"
    assert row["OrderPriceUnit"] == "EA"
    assert row["AccountAssignmentCategory"] == "K"
    assert "PurchaseRequisition" not in row
    services = row[PO_SERVICES_NAV]
    assert len(services) == 1
    svc = services[0]
    assert svc["Service"] == normalize_service_performer_code("10000000006")
    assert svc["QuantityUnit"] == "EA"
    assert svc["ConfirmedQuantity"] == "2.000"
    assert svc["NetAmount"] == "100.000"
    assert svc["NetPriceAmount"] == "200.000"
    accts = svc[PO_SERVICE_ACCT_NAV]
    assert len(accts) == 1
    assert accts[0]["CostCenter"] == "HCO91004A0"
    assert accts[0]["ServiceNumber"] == normalize_service_performer_code("10000000006")
    assert "TaxCode" not in accts[0]


def test_build_z_yser_po_sets_purg_doc_item_external_reference() -> None:
    form = sample_yser_po_form()
    form["lines"][0]["sap_po_service_ref"] = "AOLINE-TEST-001"
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="ticket-ext-01")
    svc = inner["to_PurchaseOrderItem"][0][PO_SERVICES_NAV][0]
    assert svc["PurgDocItemExternalReference"] == "AOLINE-TEST-001"


def test_build_z_yser_po_auto_generates_sap_po_service_ref() -> None:
    form = sample_yser_po_form()
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="ticket-ext-02")
    svc = inner["to_PurchaseOrderItem"][0][PO_SERVICES_NAV][0]
    assert form["lines"][0].get("sap_po_service_ref")
    assert svc["PurgDocItemExternalReference"] == form["lines"][0]["sap_po_service_ref"]


def test_form_from_z_po_read_maps_purg_doc_item_external_reference() -> None:
    svc = normalize_service_performer_code("10000000006")
    body = {
        "d": {
            "Supplier": "1000000002",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "TaxCode": "XE",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc,
                                    "ServiceEntrySheetItemDesc": "Line",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "100.000",
                                    "PurgDocItemExternalReference": "AOLINE-GET-001",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HCO91004A0",
                                                "Quantity": "1.000",
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    read_form = form_from_z_po_read(body)
    assert read_form["lines"][0]["sap_po_service_ref"] == "AOLINE-GET-001"


def test_build_z_yser_po_uses_form_payment_terms_and_unit() -> None:
    form = sample_yser_po_form()
    form["header"]["payment_terms"] = "0001"
    form["lines"][0]["order_unit"] = "KG"
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    assert inner["PaymentTerms"] == "0001"
    row = inner["to_PurchaseOrderItem"][0]
    assert row["PurchaseOrderQuantityUnit"] == "KG"
    assert row[PO_SERVICES_NAV][0]["QuantityUnit"] == "KG"


def test_build_z_yser_po_pr_linked_refs() -> None:
    form = sample_yser_po_form()
    form["header"]["payment_terms"] = "YI05"
    inner = build_z_yser_po_inner_payload(
        form=form, parent_pr_number="1010000365", ticket_id="t1"
    )
    assert inner["PaymentTerms"] == "YI05"
    row = inner["to_PurchaseOrderItem"][0]
    assert row["PurchaseRequisition"] == "1010000365"
    assert row["PurchaseRequisitionItem"] == "00010"


def test_build_z_yser_po_multi_cc() -> None:
    form = sample_yser_po_form()
    form["lines"][0]["allocations"] = [
        {"cost_center": "HCO91001H0", "qty": "1"},
        {"cost_center": "HCO91001A0", "qty": "1"},
    ]
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    row = inner["to_PurchaseOrderItem"][0]
    assert row["MultipleAcctAssgmtDistribution"] == "1"
    svc = row[PO_SERVICES_NAV][0]
    assert svc["MultipleAcctAssgmtDistribution"] == "1"
    accts = svc[PO_SERVICE_ACCT_NAV]
    assert len(accts) == 2
    assert {a["CostCenter"] for a in accts} == {"HCO91001H0", "HCO91001A0"}
    acct_qty = {a["CostCenter"]: a["Quantity"] for a in accts}
    assert acct_qty == {"HCO91001H0": "1.000", "HCO91001A0": "1.000"}
    svc_no = normalize_service_performer_code("10000000006")
    assert all(a["ServiceNumber"] == svc_no for a in accts)
    assert svc["ConfirmedQuantity"] == "2.000"


def test_build_z_yser_po_post_wraps_d_and_results() -> None:
    form = sample_yser_po_form()
    payload = build_z_yser_po_post_body(form=form, ticket_id="t1")
    assert "d" in payload
    items = payload["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    svc = items[0]["to_Services"]["results"][0]
    assert "results" in svc["to_AccountAssignment"]


def test_build_z_yser_po_maps_delivery_to_item_delivery_date() -> None:
    form = sample_yser_po_form()
    form["lines"][0]["delivery_date"] = "2026-08-15"
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    row = inner["to_PurchaseOrderItem"][0]
    assert row["DeliveryDate"] == "15.08.2026"
    svc = row[PO_SERVICES_NAV][0]
    assert svc["ServicePerformanceDate"] == "/Date(1786752000000)/"
    assert "to_ScheduleLine" not in row


def test_z_po_item_delivery_date_roundtrip_formats() -> None:
    from app.procurement.sap_po_z_payload import (
        _z_po_item_delivery_date_for_sap,
        _z_po_item_delivery_date_to_form,
    )

    assert _z_po_item_delivery_date_for_sap("2026-08-15") == "15.08.2026"
    assert _z_po_item_delivery_date_to_form("15.08.2026") == "2026-08-15"
    assert _z_po_item_delivery_date_to_form("20260815") == "2026-08-15"


def test_z_po_form_item_differs_on_service_group_change() -> None:
    from app.procurement.sap_po_z_payload import _z_po_form_item_differs

    block = {
        "service": "10000000006",
        "service_group": "SNEW-0001",
        "unit_price": "100",
        "delivery_date": "2026-08-15",
        "allocations": [{"cost_center": "CC1", "qty": "1"}],
    }
    snap = SapZPoItemSnapshot(
        item_number="10",
        is_deleted=False,
        material_group="SOLD-0001",
        services=[
            SapZPoServiceSnapshot(
                service="10000000006",
                net_price_amount="100.00",
                net_amount="100.00",
                acct_segments=[SapAcctSegment(seq="1", cost_center="CC1", quantity="1")],
            )
        ],
    )
    header: dict = {}
    assert _z_po_form_item_differs(block, snap, header=header, ticket_id=None) is True


def test_form_from_z_po_read_item_delivery_date() -> None:
    form = sample_yser_po_form()
    body = build_z_yser_po_post_body(form=form, po_number="4030010911", ticket_id="t1")
    item = body["d"]["to_PurchaseOrderItem"]["results"][0]
    item["DeliveryDate"] = "07.07.2026"
    for svc in item["to_Services"]["results"]:
        svc["ServicePerformanceDate"] = None
    read_form = form_from_z_po_read(body)
    assert read_form["lines"][0]["delivery_date"] == "2026-07-07"


def test_build_z_yser_po_omits_z_unsupported_header_fields() -> None:
    form = sample_yser_po_form()
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    assert "DocumentCurrency" not in inner


def test_build_z_yser_po_resubmit_sends_delta_only() -> None:
    form = sample_yser_po_form()
    svc_code = normalize_service_performer_code("10000000006")
    existing = [
        SapZPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            item_text="Service PO line",
            net_price_amount="200.000",
            account_assignment_cat="K",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[
                SapZPoServiceSnapshot(
                    service=svc_code,
                    confirmed_quantity="2.000",
                    net_price_amount="200.000",
                    net_amount="100.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
        SapZPoItemSnapshot(
            item_number="20",
            is_deleted=False,
            item_text="Line 20",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[SapZPoServiceSnapshot(service="10000000007", quantity_unit="EA")],
        ),
    ]
    plan = build_z_yser_po_resubmit_plan(
        form=form,
        po_number="4030000999",
        existing_items=existing,
        ticket_id="t1",
    )
    items = plan.post_body["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    deleted = items[0]
    assert deleted.get("IsReturnsItem") is True
    assert deleted["PurchaseOrderItem"] == "00020"
    assert PO_SERVICES_NAV not in deleted


def test_build_z_yser_po_resubmit_includes_changed_item_only() -> None:
    form = sample_yser_po_form()
    form["lines"][0]["unit_price"] = "120"
    form["lines"][0]["net_price"] = "240"
    svc_code = normalize_service_performer_code("10000000006")
    existing = [
        SapZPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            item_text="Service PO line",
            net_price_amount="200.000",
            account_assignment_cat="K",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[
                SapZPoServiceSnapshot(
                    service=svc_code,
                    confirmed_quantity="2.000",
                    net_price_amount="200.000",
                    net_amount="100.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
    ]
    plan = build_z_yser_po_resubmit_plan(
        form=form,
        po_number="4030000999",
        existing_items=existing,
        ticket_id="t1",
    )
    items = plan.post_body["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    assert items[0]["PurchaseOrderItem"] == "00010"
    assert items[0].get("IsReturnsItem") is False
    assert PO_SERVICES_NAV in items[0]


def test_resubmit_ignores_short_text_master_data_drift() -> None:
    """Price-only change must not resubmit unchanged lines when SAP master text differs."""
    form = sample_yser_po_form()
    form["lines"][0]["unit_price"] = "120"
    form["lines"][0]["net_price"] = "240"
    form["lines"].append(
        {
            "service": "10000000007",
            "short_text": "UI text B",
            "delivery_date": form["lines"][0]["delivery_date"],
            "unit_price": "50",
            "net_price": "100",
            "order_unit": "EA",
            "account_assignment_cat": "K",
            "allocations": [{"cost_center": "HCO91004A0", "qty": "2"}],
        }
    )
    svc_a = normalize_service_performer_code("10000000006")
    svc_b = normalize_service_performer_code("10000000007")
    existing = [
        SapZPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            item_text="UI text A",
            net_price_amount="200.000",
            account_assignment_cat="K",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[
                SapZPoServiceSnapshot(
                    service=svc_a,
                    short_text="SAP master A",
                    confirmed_quantity="2.000",
                    net_price_amount="200.000",
                    net_amount="100.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
        SapZPoItemSnapshot(
            item_number="20",
            is_deleted=False,
            item_text="UI text B",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[
                SapZPoServiceSnapshot(
                    service=svc_b,
                    short_text="SAP master B",
                    confirmed_quantity="2.000",
                    net_price_amount="100.000",
                    net_amount="50.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
    ]
    plan = build_z_yser_po_resubmit_plan(
        form=form,
        po_number="4030000999",
        existing_items=existing,
        ticket_id="t1",
    )
    items = plan.post_body["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    assert items[0]["PurchaseOrderItem"] == "00010"


def test_build_z_yser_po_maps_header_note_to_correspnc() -> None:
    form = sample_yser_po_form()
    form["header"]["header_note"] = "YSER PO header note"
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    assert inner["CorrespncInternalReference"] == "YSER PO head"
    assert inner.get("Remarks") in ("", None)


def test_build_z_yser_po_maps_requestor_email_to_sales_person() -> None:
    form = sample_yser_po_form()
    form["header"]["requestor_email"] = "indra.deo@1mg.com"
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    assert inner["SalesPerson"] == "indra.deo@1mg.com"


def test_build_z_yser_po_maps_creator_email_to_creator_mail_id() -> None:
    form = sample_yser_po_form()
    inner = build_z_yser_po_inner_payload(
        form=form, ticket_id="t1", creator_email="creator@1mg.com"
    )
    assert inner["CreatorMailId"] == "creator@1mg.com"
    assert "CreatorMailId" not in inner["to_PurchaseOrderItem"][0]


def test_build_z_yser_po_resubmit_header_only_when_sales_person_changes() -> None:
    form = sample_yser_po_form()
    form["header"]["requestor_email"] = "new.requestor@1mg.com"
    svc_code = normalize_service_performer_code("10000000006")
    existing = [
        SapZPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            item_text="Service PO line",
            net_price_amount="200.000",
            account_assignment_cat="K",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[
                SapZPoServiceSnapshot(
                    service=svc_code,
                    confirmed_quantity="2.000",
                    net_price_amount="200.000",
                    net_amount="100.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
    ]
    plan = build_z_yser_po_resubmit_plan(
        form=form,
        po_number="4030000999",
        existing_items=existing,
        ticket_id="t1",
        existing_sales_person="indra.deo@1mg.com",
    )
    inner = plan.post_body["d"]
    assert inner["SalesPerson"] == "new.requestor@1mg.com"
    assert "to_PurchaseOrderItem" in inner
    items = inner["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1


def test_build_z_yser_po_resubmit_ignores_creator_mail_id_only_change() -> None:
    form = sample_yser_po_form()
    svc_code = normalize_service_performer_code("10000000006")
    existing = [
        SapZPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            item_text="Service PO line",
            net_price_amount="200.000",
            account_assignment_cat="K",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[
                SapZPoServiceSnapshot(
                    service=svc_code,
                    confirmed_quantity="2.000",
                    net_price_amount="200.000",
                    net_amount="100.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
    ]
    with pytest.raises(ValueError, match="No YSER PO line changes"):
        build_z_yser_po_resubmit_plan(
            form=form,
            po_number="4030000999",
            existing_items=existing,
            ticket_id="t1",
            existing_sales_person="indra.deo@1mg.com",
            existing_po_texts={},
            existing_correspnc_internal_reference="YSER PO test",
            existing_creator_mail_id="",
            creator_email="creator@1mg.com",
        )


def test_form_from_z_po_read_header_note_from_correspnc() -> None:
    body = {
        "d": {
            "Supplier": "1000000002",
            "CorrespncInternalReference": "Legacy YSER",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    form = form_from_z_po_read(body)
    assert form["header"]["header_note"] == "Legacy YSER"


def test_form_from_z_po_read_falls_back_to_seed_when_correspnc_missing_on_get() -> None:
    body = {
        "d": {
            "Supplier": "1000000002",
            "CorrespncInternalReference": "",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    form = form_from_z_po_read(body, seed_form={"header": {"header_note": "seed note"}})
    assert form["header"]["header_note"] == "seed note"


def test_verify_z_po_read_checks_sales_person_when_sap_has_value() -> None:
    from app.procurement.sap_po_z_payload import verify_z_po_read_against_form

    form = sample_yser_po_form()
    form["header"]["requestor_email"] = "new@1mg.com"
    body = {"d": {"SalesPerson": "old@1mg.com", "to_PurchaseOrderItem": {"results": []}}}
    mismatches = verify_z_po_read_against_form(body, form=form, ticket_id="t1")
    assert any("SalesPerson" in m for m in mismatches)


def test_verify_z_po_read_checks_creator_mail_id_when_sap_has_value() -> None:
    from app.procurement.sap_po_z_payload import verify_z_po_read_against_form

    form = sample_yser_po_form()
    body = {
        "d": {
            "SalesPerson": "ops@1mg.com",
            "CreatorMailId": "other@1mg.com",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    mismatches = verify_z_po_read_against_form(
        body, form=form, ticket_id="t1", creator_email="creator@1mg.com"
    )
    assert any("CreatorMailId" in m for m in mismatches)


def test_verify_z_po_read_matches_multi_service_by_service_code() -> None:
    from app.procurement.sap_po_z_payload import verify_z_po_read_against_form

    svc_a = normalize_service_performer_code("10000000006")
    svc_b = normalize_service_performer_code("10000000007")
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "PurchaseOrderItemText": "Svc A",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc_a,
                                    "ServiceEntrySheetItemDesc": "Svc A",
                                    "ConfirmedQuantity": "3.000",
                                    "NetPriceAmount": "300.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HCO91001H0",
                                                "Quantity": "1.000",
                                            },
                                            {
                                                "CostCenter": "HCO91001A0",
                                                "Quantity": "2.000",
                                            },
                                        ]
                                    },
                                },
                            ]
                        },
                    },
                    {
                        "PurchaseOrderItem": "00020",
                        "PurchaseOrderItemText": "Svc B line",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc_b,
                                    "ServiceEntrySheetItemDesc": "Svc B line",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "100.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HCO91001H0",
                                                "Quantity": "1.000",
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    },
                ]
            }
        }
    }
    form = {
        "header": {"vendor": "1000000002"},
        "lines": [
            {
                "service": svc_a,
                "short_text": "Svc A",
                "net_price": "300",
                "allocations": [
                    {"cost_center": "HCO91001H0", "qty": "1"},
                    {"cost_center": "HCO91001A0", "qty": "2"},
                ],
            },
            {
                "service": svc_b,
                "short_text": "Svc B line",
                "net_price": "100",
                "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            },
        ],
    }
    assert verify_z_po_read_against_form(body, form=form) == []


def test_form_from_z_po_read_roundtrip() -> None:
    form = sample_yser_po_form()
    form["header"]["requestor_email"] = "indra.deo@1mg.com"
    body = build_z_yser_po_post_body(form=form, po_number="4030000999", ticket_id="t1")
    body["d"]["PurchaseOrder"] = "4030000999"
    body["d"]["SalesPerson"] = "indra.deo@1mg.com"
    read_form = form_from_z_po_read(body, seed_form=form)
    assert read_form["header"]["requestor_email"] == "indra.deo@1mg.com"
    line = read_form["lines"][0]
    assert line["service"] == normalize_service_performer_code("10000000006")
    assert line["order_unit"] == "EA"
    assert line["allocations"][0]["cost_center"] == "HCO91004A0"
    assert line["allocations"][0]["qty"] == "2"
    assert float(line["unit_price"]) == 100.0


def test_form_from_z_po_read_seed_delivery_and_qty() -> None:
    form = sample_yser_po_form()
    body = build_z_yser_po_post_body(form=form, po_number="4030000998", ticket_id="t1")
    body["d"]["PurchaseOrder"] = "4030000998"
    item = body["d"]["to_PurchaseOrderItem"]["results"][0]
    item["DeliveryDate"] = None
    for svc in item["to_Services"]["results"]:
        svc["ServicePerformanceDate"] = None
        for seg in svc["to_AccountAssignment"]["results"]:
            seg["Quantity"] = "2.000"
    read_form = form_from_z_po_read(body, seed_form=form)
    line = read_form["lines"][0]
    assert line["delivery_date"] == form["lines"][0]["delivery_date"]
    assert line["allocations"][0]["qty"] == "2"


def test_build_z_yser_po_resubmit_on_delivery_change() -> None:
    form = sample_yser_po_form()
    form["lines"][0]["delivery_date"] = "2026-09-01"
    svc_code = normalize_service_performer_code("10000000006")
    existing = [
        SapZPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            item_text="Service PO line",
            net_price_amount="200.000",
            account_assignment_cat="K",
            delivery_date="2026-08-15",
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            services=[
                SapZPoServiceSnapshot(
                    service=svc_code,
                    confirmed_quantity="2.000",
                    net_price_amount="200.000",
                    net_amount="100.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
    ]
    plan = build_z_yser_po_resubmit_plan(
        form=form,
        po_number="4030000999",
        existing_items=existing,
        ticket_id="t1",
    )
    items = plan.post_body["d"]["to_PurchaseOrderItem"]["results"]
    assert len(items) == 1
    assert items[0]["DeliveryDate"] == "01.09.2026"


def test_build_z_yser_po_maps_po_text_fields() -> None:
    form = sample_yser_po_form()
    form["header"]["po_remarks"] = "Line remarks"
    form["header"]["po_deadlines"] = "By Friday"
    form["header"]["po_terms_of_delivery"] = "FOB Delhi"
    inner = build_z_yser_po_inner_payload(form=form, ticket_id="t1")
    assert inner["Remarks"] == "Line remarks"
    assert inner["Deadlines"] == "By Friday"
    assert inner["TermsOfDelivery"] == "FOB Delhi"


def test_build_z_po_texts_post_body_shape() -> None:
    body = build_z_po_texts_post_body(
        po_number="4050000013",
        header={
            "po_remarks": "R",
            "po_deadlines": "D",
            "po_terms_of_delivery": "T",
        },
    )
    inner = body["d"]
    assert inner["PurchaseOrder"] == "4050000013"
    assert inner["Remarks"] == "R"
    assert inner["Deadlines"] == "D"
    assert inner["TermsOfDelivery"] == "T"
    assert inner["to_PurchaseOrderItem"]["results"] == []


def test_build_z_yser_po_resubmit_header_only_when_po_texts_change() -> None:
    form = sample_yser_po_form()
    form["header"]["po_remarks"] = "Updated remarks"
    svc_code = normalize_service_performer_code("10000000006")
    existing = [
        SapZPoItemSnapshot(
            item_number="10",
            is_deleted=False,
            item_text="Service PO line",
            net_price_amount="200.000",
            account_assignment_cat="K",
            delivery_date=_YSER_SAMPLE_DELIVERY,
            material_group=_YSER_SAMPLE_SERVICE_GROUP,
            tax_code="XE",
            services=[
                SapZPoServiceSnapshot(
                    service=svc_code,
                    confirmed_quantity="2.000",
                    net_price_amount="200.000",
                    net_amount="100.000",
                    quantity_unit="EA",
                    acct_segments=[SapAcctSegment(seq="1", cost_center="HCO91004A0", quantity="2")],
                )
            ],
        ),
    ]
    plan = build_z_yser_po_resubmit_plan(
        form=form,
        po_number="4030000999",
        existing_items=existing,
        ticket_id="t1",
        existing_sales_person="",
        existing_po_texts={"Remarks": "", "Deadlines": "", "TermsOfDelivery": ""},
    )
    inner = plan.post_body["d"]
    assert inner["Remarks"] == "Updated remarks"
    assert len(inner["to_PurchaseOrderItem"]["results"]) == 1


def test_form_from_z_po_read_maps_po_text_fields() -> None:
    form = sample_yser_po_form()
    body = {
        "d": {
            "CorrespncInternalReference": "SAP header note",
            "Remarks": "SAP remarks",
            "Deadlines": "SAP deadlines",
            "TermsOfDelivery": "SAP terms",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    read_form = form_from_z_po_read(body, seed_form=form)
    assert read_form["header"]["header_note"] == "SAP header note"
    assert read_form["header"]["po_remarks"] == "SAP remarks"
    assert read_form["header"]["po_deadlines"] == "SAP deadlines"
    assert read_form["header"]["po_terms_of_delivery"] == "SAP terms"


def test_po_texts_differ_detects_change() -> None:
    header = {"po_remarks": "new"}
    sap_root = {"Remarks": "old", "Deadlines": "", "TermsOfDelivery": ""}
    assert po_texts_differ(header, sap_root) is True


def test_form_from_z_po_read_grouped_uses_nested_acct_per_service() -> None:
    """Grouped multi-service PO: CC from each service's nested ``to_AccountAssignment``."""
    svc_a = normalize_service_performer_code("10000000006")
    svc_b = normalize_service_performer_code("10000000007")
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "MaterialGroup": "S089-0001",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc_a,
                                    "ServiceEntrySheetItemDesc": "PO-SVC-A",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "100.000",
                                    "NetAmount": "100.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A0",
                                                "Quantity": "1.000",
                                            }
                                        ]
                                    },
                                },
                                {
                                    "Service": svc_b,
                                    "ServiceEntrySheetItemDesc": "PO-SVC-B",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "80.000",
                                    "NetAmount": "80.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A1",
                                                "Quantity": "1.000",
                                            }
                                        ]
                                    },
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    read_form = form_from_z_po_read(body)
    assert len(read_form["lines"]) == 2
    assert read_form["lines"][0]["allocations"] == [
        {"cost_center": "HBM13021A0", "qty": "1"}
    ]
    assert read_form["lines"][1]["allocations"] == [
        {"cost_center": "HBM13021A1", "qty": "1"}
    ]
    assert read_form["lines"][0]["net_price"] == "100.00"
    assert read_form["lines"][1]["net_price"] == "80.00"


def test_form_from_z_po_read_dup_service_filters_acct_by_unit_times_qty() -> None:
    """Duplicate service code: keep acct rows where PurgDocNetAmount ≈ unit_price × qty."""
    svc = normalize_service_performer_code("10000000006")
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc,
                                    "ServiceEntrySheetItemDesc": "DUP-A",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "50.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A0",
                                                "Quantity": "1.000",
                                                "PurgDocNetAmount": "50.000",
                                            },
                                            {
                                                "CostCenter": "HBM13021A1",
                                                "Quantity": "1.000",
                                                "PurgDocNetAmount": "60.000",
                                            },
                                        ]
                                    },
                                },
                                {
                                    "Service": svc,
                                    "ServiceEntrySheetItemDesc": "DUP-B",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "60.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A0",
                                                "Quantity": "1.000",
                                                "PurgDocNetAmount": "50.000",
                                            },
                                            {
                                                "CostCenter": "HBM13021A1",
                                                "Quantity": "1.000",
                                                "PurgDocNetAmount": "60.000",
                                            },
                                        ]
                                    },
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    read_form = form_from_z_po_read(body)
    assert read_form["lines"][0]["allocations"] == [
        {"cost_center": "HBM13021A0", "qty": "1"}
    ]
    assert read_form["lines"][1]["allocations"] == [
        {"cost_center": "HBM13021A1", "qty": "1"}
    ]


def test_form_from_z_po_read_simple_single_cc_uses_service_qty() -> None:
    """Single-service PO: qty from ``ConfirmedQuantity`` (services tab), CC from acct row."""
    svc = normalize_service_performer_code("10000000006")
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc,
                                    "ServiceEntrySheetItemDesc": "Simple svc",
                                    "ConfirmedQuantity": "2.000",
                                    "NetPriceAmount": "200.000",
                                    "NetAmount": "100.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HCO91004A0",
                                                "Quantity": "1.000",
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    read_form = form_from_z_po_read(body)
    line = read_form["lines"][0]
    assert line["allocations"] == [{"cost_center": "HCO91004A0", "qty": "2"}]
    assert line["net_price"] == "200.00"
    assert float(line["unit_price"]) == 100.0


def test_form_from_z_po_read_fractional_acct_qty_preserved() -> None:
    """SAP 50/50 CC split: preserve decimal acct ``Quantity`` (e.g. PO 4030010245)."""
    svc = normalize_service_performer_code("100000000226")
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc,
                                    "ServiceEntrySheetItemDesc": "PEST Control",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "1200.000",
                                    "NetAmount": "1200.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13215A0",
                                                "Quantity": "0.500",
                                                "PurgDocNetAmount": "600.000",
                                            },
                                            {
                                                "CostCenter": "HBM13223A0",
                                                "Quantity": "0.500",
                                                "PurgDocNetAmount": "600.000",
                                            },
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    read_form = form_from_z_po_read(body)
    assert read_form["lines"][0]["allocations"] == [
        {"cost_center": "HBM13215A0", "qty": "0.5"},
        {"cost_center": "HBM13223A0", "qty": "0.5"},
    ]


def test_form_from_z_po_read_dup_same_cc_refines_by_confirmed_qty() -> None:
    """Dup service + same CC fan-out: keep acct row whose qty matches ConfirmedQuantity."""
    svc = normalize_service_performer_code("10000001179")
    cc = "TCO91001H0"

    def _svc(cq: str, net: str, unit: str, acct_rows: list[dict[str, str]]) -> dict:
        return {
            "Service": svc,
            "ServiceEntrySheetItemDesc": "Disprz fee",
            "ConfirmedQuantity": cq,
            "NetPriceAmount": net,
            "NetAmount": unit,
            "to_AccountAssignment": {"results": acct_rows},
        }

    shared_accts = [
        {"CostCenter": cc, "Quantity": "1481.000", "PurgDocNetAmount": "37025.000"},
        {"CostCenter": cc, "Quantity": "2000.000", "PurgDocNetAmount": "54000.000"},
        {"CostCenter": cc, "Quantity": "283.000", "PurgDocNetAmount": "7075.000"},
        {"CostCenter": cc, "Quantity": "887.000", "PurgDocNetAmount": "22175.000"},
    ]
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "to_Services": {
                            "results": [
                                _svc("1481.000", "37025.000", "25.000", shared_accts),
                                _svc("2000.000", "54000.000", "27.000", shared_accts),
                                _svc("283.000", "7075.000", "25.000", shared_accts),
                            ]
                        },
                    }
                ]
            }
        }
    }
    read_form = form_from_z_po_read(body)
    assert len(read_form["lines"]) == 3
    assert read_form["lines"][0]["allocations"] == [{"cost_center": cc, "qty": "1481"}]
    assert read_form["lines"][1]["allocations"] == [{"cost_center": cc, "qty": "2000"}]
    assert read_form["lines"][2]["allocations"] == [{"cost_center": cc, "qty": "283"}]


def test_form_from_z_po_read_complex_po_4030011121_amount_filter() -> None:
    """Live SAP dump: dup+unique services, multi-CC fan-out — unit×qty acct filter."""
    import json
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / ".tmp_po_4030011121_full_get.json"
    )
    data = json.loads(fixture.read_text())
    body = {"d": {"to_PurchaseOrderItem": {"results": data["items"]}}}
    read_form = form_from_z_po_read(body)
    assert len(read_form["lines"]) == 3
    def _alloc_pairs(rows: list[dict]) -> list[tuple[str, str]]:
        return [(a["cost_center"], a["qty"]) for a in rows[:2]]

    assert _alloc_pairs(read_form["lines"][0]["allocations"]) == [
        ("HBM13021A0", "1"),
        ("HBM13021A1", "2"),
    ]
    assert _alloc_pairs(read_form["lines"][1]["allocations"]) == [
        ("HBM13021A0", "1"),
        ("HBM13021A1", "1"),
    ]
    assert _alloc_pairs(read_form["lines"][2]["allocations"]) == [
        ("HBM13021A1", "1"),
        ("HBM13021A0", "2"),
    ]
    for ln in read_form["lines"]:
        for alloc in ln["allocations"]:
            assert alloc.get("po_acct_assgmt_number")


def test_form_from_z_po_read_hydrates_plant_when_service_group_set() -> None:
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "Plant": "H001",
                        "StorageLocation": "3021",
                        "MaterialGroup": "S089-0001",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000001000000000",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "100.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A0",
                                                "Quantity": "1.000",
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = form_from_z_po_read(body)
    assert form["header"].get("plant") == "H001"
    assert form["header"].get("storage_location") == "3021"


def test_reconcile_yser_po_verify_form_copies_sap_allocations() -> None:
    from app.procurement.sap_po_z_payload import reconcile_yser_po_verify_form_with_sap_read

    verify_form = {
        "header": {},
        "lines": [
            {
                "service": "000000001000000000",
                "sap_po_service_ref": "REF-A",
                "allocations": [{"cost_center": "HBM13021A0", "qty": "1"}],
            }
        ],
    }
    after_body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": "000000001000000000",
                                    "PurgDocItemExternalReference": "REF-A",
                                    "ConfirmedQuantity": "2.000",
                                    "NetPriceAmount": "200.000",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A0",
                                                "Quantity": "2.000",
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    rec = reconcile_yser_po_verify_form_with_sap_read(verify_form, after_body)
    assert rec["lines"][0]["allocations"][0]["qty"] == "2"


def test_verify_z_po_read_matches_dup_services_by_external_ref() -> None:
    from app.procurement.sap_po_z_payload import verify_z_po_read_against_form

    svc = normalize_service_performer_code("10000000006")
    body = {
        "d": {
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "to_Services": {
                            "results": [
                                {
                                    "Service": svc,
                                    "ServiceEntrySheetItemDesc": "REF-A",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "50.000",
                                    "PurgDocItemExternalReference": "AOLINE-MATCH-001",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A0",
                                                "Quantity": "1.000",
                                                "PurgDocNetAmount": "50.000",
                                            }
                                        ]
                                    },
                                },
                                {
                                    "Service": svc,
                                    "ServiceEntrySheetItemDesc": "REF-B",
                                    "ConfirmedQuantity": "1.000",
                                    "NetPriceAmount": "60.000",
                                    "PurgDocItemExternalReference": "AOLINE-MATCH-002",
                                    "to_AccountAssignment": {
                                        "results": [
                                            {
                                                "CostCenter": "HBM13021A1",
                                                "Quantity": "1.000",
                                                "PurgDocNetAmount": "60.000",
                                            }
                                        ]
                                    },
                                },
                            ]
                        },
                    }
                ]
            }
        }
    }
    form = {
        "header": {},
        "lines": [
            {
                "service": "10000000006",
                "short_text": "wrong text",
                "net_price": "99.00",
                "sap_po_service_ref": "AOLINE-MATCH-002",
                "allocations": [{"cost_center": "HBM13021A1", "qty": "1"}],
            },
            {
                "service": "10000000006",
                "short_text": "also wrong",
                "net_price": "88.00",
                "sap_po_service_ref": "AOLINE-MATCH-001",
                "allocations": [{"cost_center": "HBM13021A0", "qty": "1"}],
            },
        ],
    }
    assert verify_z_po_read_against_form(body, form=form) == []


def test_verify_po_texts_against_form() -> None:
    mismatches = verify_po_texts_against_form(
        {"Remarks": "a", "Deadlines": "b", "TermsOfDelivery": "c"},
        header={"po_remarks": "x", "po_deadlines": "b", "po_terms_of_delivery": "c"},
    )
    assert any("Remarks" in m for m in mismatches)


def test_normalize_form_preserves_purchase_order_item() -> None:
    from app.procurement.field_schema import normalize_form

    form = {
        "header": {"plant": "H001"},
        "lines": [
            {
                "service": "000000001000000000",
                "purchase_order_item": "10",
                "sap_po_service_ref": "AOLINE-TEST-001",
                "allocations": [{"cost_center": "HBM13021A0", "qty": "1"}],
            }
        ],
    }
    out = normalize_form("YSER", form)
    ln = out["lines"][0]
    assert ln.get("purchase_order_item") == "10"
    assert ln.get("sap_po_service_ref") == "AOLINE-TEST-001"


def _po_update_header() -> dict[str, str]:
    return {
        "purchasing_org": "1MGH",
        "purchasing_group": "A0B",
        "plant": "H001",
        "vendor": "8005",
    }


def test_build_yser_po_update_posts_add_service() -> None:
    import copy

    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-OLD",
                "purchase_order_item": "10",
                "unit_price": "100",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"].append(
        {
            "service": "SVC002",
            "service_group": "G1",
            "purchase_order_item": "10",
            "unit_price": "50",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        }
    )
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011000",
        sap_item_count=1,
        ticket_id="t-add-svc",
    )
    new_svc = [
        svc
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
        for svc in item["to_Services"]["results"]
        if svc.get("Service") == normalize_service_performer_code("SVC002")
        and svc.get("IsDeleted") != "X"
    ]
    assert new_svc
    assert new_svc[0].get("PurgDocItemExternalReference")
    assert len(posts) == 1


def test_build_yser_po_update_posts_service_delete() -> None:
    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC002",
                "service_group": "G1",
                "sap_po_service_ref": "REF-B",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    submitted = {"header": sap_form["header"], "lines": [sap_form["lines"][0]]}
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011001",
        sap_item_count=1,
        ticket_id="t-del-svc",
    )
    deleted = [
        svc
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
        for svc in item["to_Services"]["results"]
        if svc.get("IsDeleted") == "X"
    ]
    assert deleted
    assert deleted[0].get("PurgDocItemExternalReference") == "REF-B"
    kept = [
        svc
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
        for svc in item["to_Services"]["results"]
        if svc.get("IsDeleted") != "X"
    ]
    assert kept == []


def test_build_yser_po_update_posts_cc_delete() -> None:
    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-CC",
                "purchase_order_item": "10",
                "unit_price": "100",
                "allocations": [
                    {"cost_center": "CC1", "qty": "1"},
                    {"cost_center": "CC2", "qty": "1"},
                ],
            }
        ],
    }
    submitted = {
        "header": sap_form["header"],
        "lines": [
            {
                **sap_form["lines"][0],
                "allocations": [{"cost_center": "CC1", "qty": "2"}],
            }
        ],
    }
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011002",
        sap_item_count=1,
        ticket_id="t-cc-del",
    )
    deleted = [
        acct
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
        for svc in item["to_Services"]["results"]
        for acct in svc["to_AccountAssignment"]["results"]
        if acct.get("IsDeleted") is True
    ]
    assert deleted
    assert any(a.get("CostCenter") == "CC2" for a in deleted)
    svc_rows = [
        svc
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
        for svc in item["to_Services"]["results"]
    ]
    assert svc_rows[0].get("MultipleAcctAssgmtDistribution") == "0"


def test_build_yser_po_update_posts_header_note_only() -> None:
    import copy

    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    sap_form = {
        "header": {**_po_update_header(), "header_note": "old note"},
        "lines": [
            {
                "service": "SVC001",
                "sap_po_service_ref": "REF-NOTE",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["header"]["header_note"] = "new note"
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011003",
        sap_item_count=1,
        ticket_id="t-note",
    )
    assert len(posts) == 1
    assert posts[0]["d"].get("CorrespncInternalReference") == "new note"


def test_build_yser_po_update_posts_field_only_single_post() -> None:
    import copy

    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "sap_po_service_ref": "REF-FLD",
                "purchase_order_item": "10",
                "unit_price": "100",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["unit_price"] = "120"
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011004",
        sap_item_count=1,
        ticket_id="t-fld",
    )
    assert len(posts) == 1
    svc = posts[0]["d"]["to_PurchaseOrderItem"]["results"][0]["to_Services"]["results"][0]
    assert svc.get("IsDeleted") != "X"


def _item_tax_from_posts(posts: list[dict]) -> list[str]:
    taxes: list[str] = []
    for post in posts:
        items = post["d"]["to_PurchaseOrderItem"]
        if isinstance(items, dict):
            items = items.get("results") or []
        for item in items:
            tc = item.get("TaxCode")
            if tc:
                taxes.append(str(tc))
    return taxes


def test_build_yser_po_update_posts_grouped_line2_tax_only() -> None:
    import copy

    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "tax_code": "FA",
                "unit_price": "100",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC002",
                "service_group": "G1",
                "sap_po_service_ref": "REF-B",
                "purchase_order_item": "10",
                "tax_code": "FA",
                "unit_price": "80",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][1]["tax_code"] = "WA"
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011005",
        sap_item_count=1,
        ticket_id="t-tax-l2",
    )
    assert posts
    assert "WA" in _item_tax_from_posts(posts)


def test_build_z_yser_po_resubmit_plan_grouped_line2_tax_only() -> None:
    import copy

    from app.procurement.sap_po_z_payload import build_z_yser_po_resubmit_plan

    snap = SapZPoItemSnapshot(
        item_number="10",
        is_deleted=False,
        tax_code="FA",
        material_group="G1",
        services=[
            SapZPoServiceSnapshot(service="SVC001"),
            SapZPoServiceSnapshot(service="SVC002"),
        ],
    )
    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "tax_code": "FA",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC002",
                "service_group": "G1",
                "sap_po_service_ref": "REF-B",
                "purchase_order_item": "10",
                "tax_code": "FA",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][1]["tax_code"] = "WA"
    plan = build_z_yser_po_resubmit_plan(
        form=submitted,
        po_number="4030011006",
        existing_items=[snap],
        ticket_id="t-tax-resubmit",
    )
    assert "WA" in _item_tax_from_posts([plan.post_body])


def test_yser_po_create_reconcile_detects_missing_service() -> None:
    import copy

    from app.procurement.sap_po_z_update import yser_po_has_structural_delta

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"].append(
        {
            "service": "SVC002",
            "service_group": "G1",
            "sap_po_service_ref": "REF-B",
            "purchase_order_item": "10",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        }
    )
    assert yser_po_has_structural_delta(sap_form=sap_form, submitted=submitted)


def test_yser_po_create_reconcile_needed_only_missing_services() -> None:
    import copy

    from app.procurement.sap_po_z_update import (
        yser_po_create_reconcile_needed,
        yser_po_has_structural_delta,
    )

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"][0]["allocations"] = [{"cost_center": "CC2", "qty": "1"}]
    assert yser_po_has_structural_delta(sap_form=sap_form, submitted=submitted)
    assert not yser_po_create_reconcile_needed(sap_form=sap_form, submitted=submitted)


def test_build_yser_po_update_posts_create_reconcile_new_po_item() -> None:
    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    submitted = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC-A",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC-C",
                "service_group": "G2",
                "sap_po_service_ref": "REF-C",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC-D",
                "service_group": "G2",
                "sap_po_service_ref": "REF-D",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC-A",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011299",
        sap_item_count=1,
        ticket_id="t-reconcile-new-item",
        create_reconcile=True,
        existing_items=[
            type("Snap", (), {"item_number": "10", "is_deleted": False})(),
        ],
    )
    new_item_rows = [
        item
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
        if item.get("PurchaseOrderItem") == "00020"
    ]
    assert len(new_item_rows) == 1
    svc_refs = {
        svc.get("PurgDocItemExternalReference")
        for svc in new_item_rows[0]["to_Services"]["results"]
        if svc.get("IsDeleted") != "X"
    }
    assert svc_refs == {"REF-C", "REF-D"}


@pytest.mark.asyncio
async def test_reconcile_yser_po_services_after_create_runs_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import copy
    from unittest.mock import AsyncMock, MagicMock

    from app.procurement import sap_po_client

    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    submitted = copy.deepcopy(sap_form)
    submitted["lines"].append(
        {
            "service": "SVC002",
            "service_group": "G1",
            "sap_po_service_ref": "REF-B",
            "purchase_order_item": "10",
            "allocations": [{"cost_center": "CC1", "qty": "1"}],
        }
    )

    update_mock = AsyncMock(return_value=("4030011999", None))
    monkeypatch.setattr(sap_po_client, "_sap_z_update_yser_po_once", update_mock)
    monkeypatch.setattr(
        sap_po_client,
        "_sap_get_po_body",
        AsyncMock(return_value=({"d": {}}, None)),
    )
    monkeypatch.setattr(
        sap_po_client,
        "_credentials_or_error",
        lambda: (("https://example.test", "user", "pass"), None),
    )
    monkeypatch.setattr(
        "app.procurement.sap_po_z_payload.form_from_z_po_read",
        lambda _body, **_: copy.deepcopy(sap_form),
    )
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(sap_po_client.httpx, "AsyncClient", lambda **_: mock_client)

    err = await sap_po_client._reconcile_yser_po_services_after_create(
        po_number="4030011999",
        form=submitted,
        ticket_id="t-reconcile",
        parent_pr_number="1010000001",
        creator_email="ops@example.com",
    )
    assert err is None
    update_mock.assert_awaited_once()
    kwargs = update_mock.await_args.kwargs
    assert kwargs["po_number"] == "4030011999"
    assert kwargs["parent_pr_number"] == "1010000001"
    assert kwargs["creator_email"] == "ops@example.com"
    assert kwargs.get("create_reconcile") is True


def test_stamp_yser_po_grouped_item_numbers_multi_group() -> None:
    from app.procurement.sap_po_z_payload import stamp_yser_po_grouped_item_numbers

    form = {
        "header": _po_update_header(),
        "lines": [
            {"service": "SVC-A", "service_group": "G1", "allocations": [{"cost_center": "CC1", "qty": "1"}]},
            {"service": "SVC-B", "service_group": "G1", "allocations": [{"cost_center": "CC1", "qty": "1"}]},
            {"service": "SVC-C", "service_group": "G2", "allocations": [{"cost_center": "CC1", "qty": "1"}]},
            {"service": "SVC-D", "service_group": "G2", "allocations": [{"cost_center": "CC1", "qty": "1"}]},
        ],
    }
    stamp_yser_po_grouped_item_numbers(form, force=True)
    items = [ln["purchase_order_item"] for ln in form["lines"]]
    assert items == ["00010", "00010", "00020", "00020"]


def test_prepare_yser_po_create_reconcile_pairs_by_ref_not_service_code() -> None:
    import copy

    from app.procurement.sap_po_z_payload import prepare_yser_po_create_reconcile_forms

    submitted = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "short_text": "Line one",
                "sap_po_service_ref": "REF-A",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC001",
                "service_group": "G1",
                "short_text": "Line two",
                "sap_po_service_ref": "REF-B",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "short_text": "Line one",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }
    sub_prep, sap_prep = prepare_yser_po_create_reconcile_forms(
        submitted, sap_form=sap_form
    )
    assert sub_prep["lines"][0]["purchase_order_item"] == "00010"
    assert sub_prep["lines"][1]["purchase_order_item"] == "00010"
    assert sap_prep["lines"][0]["sap_po_service_ref"] == "REF-A"
    assert sap_prep["lines"][0]["purchase_order_item"] == "00010"


def test_build_yser_po_update_posts_create_reconcile_multi_group() -> None:
    import copy

    from app.procurement.sap_po_z_update import build_yser_po_update_posts

    submitted = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC-A",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "unit_price": "100",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC-B",
                "service_group": "G1",
                "sap_po_service_ref": "REF-B",
                "unit_price": "50",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC-C",
                "service_group": "G2",
                "sap_po_service_ref": "REF-C",
                "unit_price": "80",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC-D",
                "service_group": "G2",
                "sap_po_service_ref": "REF-D",
                "unit_price": "40",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    sap_form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC-A",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "unit_price": "100",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
            {
                "service": "SVC-C",
                "service_group": "G2",
                "sap_po_service_ref": "REF-C",
                "purchase_order_item": "20",
                "unit_price": "80",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            },
        ],
    }
    posts, _ = build_yser_po_update_posts(
        submitted=submitted,
        sap_form=sap_form,
        po_number="4030011263",
        sap_item_count=2,
        ticket_id="t-reconcile-mg",
        create_reconcile=True,
        existing_items=[
            type("Snap", (), {"item_number": "10", "is_deleted": False})(),
            type("Snap", (), {"item_number": "20", "is_deleted": False})(),
        ],
    )
    added_refs = [
        svc.get("PurgDocItemExternalReference")
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
        for svc in item["to_Services"]["results"]
        if svc.get("IsDeleted") != "X"
    ]
    assert set(added_refs) == {"REF-B", "REF-D"}
    item_nums = {
        item.get("PurchaseOrderItem")
        for post in posts
        for item in post["d"]["to_PurchaseOrderItem"]["results"]
    }
    assert item_nums == {"00010", "00020"}
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_reconcile_yser_po_services_after_create_skips_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import copy
    from unittest.mock import AsyncMock, MagicMock

    from app.procurement import sap_po_client

    form = {
        "header": _po_update_header(),
        "lines": [
            {
                "service": "SVC001",
                "service_group": "G1",
                "sap_po_service_ref": "REF-A",
                "purchase_order_item": "10",
                "allocations": [{"cost_center": "CC1", "qty": "1"}],
            }
        ],
    }

    update_mock = AsyncMock(return_value=("4030011999", None))
    monkeypatch.setattr(sap_po_client, "_sap_z_update_yser_po_once", update_mock)
    monkeypatch.setattr(
        sap_po_client,
        "_sap_get_po_body",
        AsyncMock(return_value=({"d": {}}, None)),
    )
    monkeypatch.setattr(
        sap_po_client,
        "_credentials_or_error",
        lambda: (("https://example.test", "user", "pass"), None),
    )
    monkeypatch.setattr(
        "app.procurement.sap_po_z_payload.form_from_z_po_read",
        lambda _body, **_: copy.deepcopy(form),
    )
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(sap_po_client.httpx, "AsyncClient", lambda **_: mock_client)

    err = await sap_po_client._reconcile_yser_po_services_after_create(
        po_number="4030011999",
        form=form,
        ticket_id="t-reconcile-skip",
    )
    assert err is None
    update_mock.assert_not_awaited()

