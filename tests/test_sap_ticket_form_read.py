"""SAP GET → AgentOS form mapping for ticket display."""

from __future__ import annotations

from app.procurement import sap_pr_payload
from app.procurement.sap_ticket_form_read import (
    form_from_po_sap_read,
    form_from_pr_sap_read,
    merge_po_form_from_seed,
)


def test_form_from_pr_sap_read_maps_items_and_acct() -> None:
    body = {
        "d": {
            "PurchasingOrganization": "1MGH",
            "PurReqnDescription": "PR note",
            "to_PurchaseReqnItem": {
                "results": [
                    {
                        "PurchaseRequisitionItem": "10",
                        "Material": "4200000027",
                        "Plant": "H001",
                        "StorageLocation": "3021",
                        "MaterialGroup": "SD05-0001",
                        "PurchasingGroup": "A0B",
                        "RequestedQuantity": "2.000",
                        "BaseUnit": "KG",
                        "PurchaseRequisitionPrice": "99.50",
                        "PurchaseRequisitionItemText": "Line A",
                        "DeliveryDate": "/Date(1745971200000)/",
                        "AccountAssignmentCategory": "K",
                        "PurchasingDocumentItemCategory": "0",
                        "to_PurchaseReqnAcctAssgmt": {
                            "results": [
                                {
                                    "PurchaseReqnAcctAssgmtNumber": "1",
                                    "CostCenter": "HCO91001H0",
                                    "Quantity": "2.000",
                                }
                            ]
                        },
                    }
                ]
            },
        }
    }
    form = form_from_pr_sap_read(body, document_type="YUNB")
    assert form["header"]["purchasing_org"] == "1MGH"
    assert form["header"]["header_note"] == "PR note"
    assert len(form["lines"]) == 1
    line = form["lines"][0]
    assert line["purchase_requisition_item"] == "10"
    assert line["material"] == "4200000027"
    assert line["order_unit"] == "KG"
    assert line["unit_price"] == "99.50"
    assert line["valuation_price"] == "199"
    assert line["gross_price"] == "99.50"
    assert line["allocations"][0]["cost_center"] == "HCO91001H0"
    assert line["material_group"] == "SD05-0001"
    assert form["header"]["material_group"] == "SD05-0001"


def test_form_from_pr_sap_read_strips_legacy_trace_markers() -> None:
    body = {
        "d": {
            "PurReqnDescription": "OPS note [AO:ABCDEF0123456789ABCDEF0123456789]",
            "to_PurchaseReqnItem": {
                "results": [
                    {
                        "PurchaseRequisitionItem": "10",
                        "Material": "4200000027",
                        "PurchaseRequisitionItemText": (
                            f"Line desc{sap_pr_payload.pr_item_trace_marker('e5b66b62-758a-4d44-b2cb-69671599ce1a')}"
                        ),
                        "to_PurchaseReqnAcctAssgmt": {
                            "results": [{"CostCenter": "HCO91001H0", "Quantity": "1.000"}]
                        },
                    }
                ]
            },
        }
    }
    form = form_from_pr_sap_read(body, document_type="YUNB")
    assert form["header"]["header_note"] == "OPS note"
    assert form["lines"][0]["short_text"] == "Line desc"


def test_form_from_po_sap_read_multi_cc() -> None:
    body = {
        "d": {
            "Supplier": "1000000002",
            "PurchasingOrganization": "1MGH",
            "PurchasingGroup": "A0B",
            "PaymentTerms": "YI10",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "10",
                        "Material": "4200000027",
                        "OrderQuantity": "3.000",
                        "PurchaseOrderQuantityUnit": "KG",
                        "NetPriceAmount": "25.00",
                        "Plant": "H001",
                        "AccountAssignmentCategory": "K",
                        "PurchaseRequisition": "1040000063",
                        "PurchaseRequisitionItem": "10",
                        "to_PurchaseOrderAccountAssignment": {
                            "results": [
                                {
                                    "AccountAssignmentNumber": "1",
                                    "CostCenter": "CC1",
                                    "Quantity": "1.000",
                                },
                                {
                                    "AccountAssignmentNumber": "2",
                                    "CostCenter": "CC2",
                                    "Quantity": "2.000",
                                },
                            ]
                        },
                    }
                ]
            },
        }
    }
    form = form_from_po_sap_read(body, document_type="YUNB")
    assert form["header"]["vendor"] == "1000000002"
    assert form["header"]["payment_terms"] == "YI10"
    line = form["lines"][0]
    assert line["purchase_requisition_item"] == "10"
    assert line["order_unit"] == "KG"
    assert line["unit_price"] == "25.00"
    assert line["valuation_price"] == "75"
    assert len(line["allocations"]) == 2


def test_form_from_po_sap_read_maps_po_text_fields() -> None:
    body = {
        "d": {
            "Supplier": "1000000002",
            "PurchasingOrganization": "1LFS",
            "PurchasingGroup": "A0B",
            "Remarks": "Material remarks",
            "Deadlines": "Material deadlines",
            "TermsOfDelivery": "Material terms",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    form = form_from_po_sap_read(body, document_type="YUNB")
    assert form["header"]["po_remarks"] == "Material remarks"
    assert form["header"]["po_deadlines"] == "Material deadlines"
    assert form["header"]["po_terms_of_delivery"] == "Material terms"


def test_form_from_po_sap_read_maps_requestor_email_from_salesperson() -> None:
    body = {
        "d": {
            "Supplier": "1000000002",
            "SupplierRespSalesPersonName": "ops@1mg.com",
            "IncotermsLocation1": "creator@1mg.com",
            "to_PurchaseOrderItem": {"results": []},
        }
    }
    form = form_from_po_sap_read(body, document_type="YUNB")
    assert form["header"]["requestor_email"] == "ops@1mg.com"


def test_merge_po_form_from_seed_yser_fills_empty_cc_from_seed() -> None:
    """Grouped YSER PO cold-read may omit CC; seed merge restores per-service CC."""
    form = {
        "header": {},
        "lines": [
            {
                "service": "10000000006",
                "purchase_order_item": "10",
                "net_price": "100.00",
                "allocations": [{"cost_center": "", "qty": "1"}],
            },
            {
                "service": "10000000007",
                "purchase_order_item": "10",
                "net_price": "80.00",
                "allocations": [{"cost_center": "", "qty": "1"}],
            },
        ],
    }
    seed = {
        "lines": [
            {
                "service": "10000000006",
                "purchase_order_item": "10",
                "delivery_date": "2026-09-01",
                "allocations": [{"cost_center": "HBM13021A0", "qty": "1"}],
            },
            {
                "service": "10000000007",
                "purchase_order_item": "10",
                "delivery_date": "2026-09-01",
                "allocations": [{"cost_center": "HBM13021A1", "qty": "1"}],
            },
        ],
    }
    merged = merge_po_form_from_seed(form, document_type="YSER", seed_form=seed)
    assert merged["lines"][0]["allocations"] == [{"cost_center": "HBM13021A0", "qty": "1"}]
    assert merged["lines"][1]["allocations"] == [{"cost_center": "HBM13021A1", "qty": "1"}]
    assert merged["lines"][0]["delivery_date"] == "2026-09-01"
