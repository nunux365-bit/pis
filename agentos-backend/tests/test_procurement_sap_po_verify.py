"""PO verify helpers — header note and delivery date."""

from __future__ import annotations

from app.procurement.sap_po_payload import verify_po_read_against_form


def test_verify_po_skips_empty_correspnc_when_form_has_note() -> None:
    form = {
        "header": {
            "purchasing_org": "1MGH",
            "purchasing_group": "S0Z",
            "vendor": "1000006465|1MGH",
            "tax_code": "HA",
            "header_note": "ad9d5852908",
        },
        "lines": [
            {
                "material": "4000000002",
                "material_group": "M020-0001",
                "short_text": "line",
                "delivery_date": "2026-08-17",
                "unit_price": "10",
                "net_price": "10",
                "order_unit": "PC",
                "allocations": [{"cost_center": "HBM11001A0", "qty": "1"}],
            }
        ],
    }
    body = {
        "d": {
            "CorrespncInternalReference": "",
            "Supplier": "1000006465",
            "PurchasingOrganization": "1MGH",
            "PurchasingGroup": "S0Z",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4000000002",
                        "MaterialGroup": "M020-0001",
                        "OrderQuantity": "1",
                        "NetPriceAmount": "10.00",
                        "PurchaseOrderQuantityUnit": "PC",
                        "to_ScheduleLine": {
                            "results": [
                                {
                                    "ScheduleLineDeliveryDate": "/Date(1786924800000)/",
                                }
                            ]
                        },
                        "to_PurchaseOrderAccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "HBM11001A0",
                                    "Quantity": "1",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    mismatches = verify_po_read_against_form(
        body, form=form, document_type="YUNB", ticket_id="t1"
    )
    assert not any("CorrespncInternalReference" in m for m in mismatches)


def test_verify_po_flags_delivery_date_mismatch() -> None:
    form = {
        "header": {
            "purchasing_org": "1MGH",
            "purchasing_group": "S0Z",
            "vendor": "1000006465|1MGH",
            "tax_code": "HA",
        },
        "lines": [
            {
                "material": "4000000002",
                "material_group": "M020-0001",
                "short_text": "line",
                "delivery_date": "2026-08-17",
                "unit_price": "10",
                "net_price": "10",
                "order_unit": "PC",
                "allocations": [{"cost_center": "HBM11001A0", "qty": "1"}],
            }
        ],
    }
    body = {
        "d": {
            "Supplier": "1000006465",
            "PurchasingOrganization": "1MGH",
            "PurchasingGroup": "S0Z",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4000000002",
                        "MaterialGroup": "M020-0001",
                        "OrderQuantity": "1",
                        "NetPriceAmount": "10.00",
                        "PurchaseOrderQuantityUnit": "PC",
                        "to_ScheduleLine": {
                            "results": [
                                {
                                    "ScheduleLineDeliveryDate": "/Date(1786752000000)/",
                                }
                            ]
                        },
                        "to_PurchaseOrderAccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "HBM11001A0",
                                    "Quantity": "1",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    mismatches = verify_po_read_against_form(
        body, form=form, document_type="YUNB", ticket_id="t1"
    )
    assert any("delivery_date" in m for m in mismatches)
