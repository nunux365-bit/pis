"""Per-line tax code helpers."""

from __future__ import annotations

from app.procurement.field_schema import default_empty_block, normalize_form
from app.procurement.line_tax_code import (
    ensure_form_line_tax_codes,
    line_tax_code,
    sync_header_tax_code,
)
from app.procurement.sap_po_payload import build_po_payload


def test_line_tax_code_prefers_block_over_header() -> None:
    block = {"tax_code": "FA"}
    header = {"tax_code": "XE"}
    assert line_tax_code(block, header) == "FA"


def test_line_tax_code_falls_back_to_header() -> None:
    assert line_tax_code({"tax_code": ""}, {"tax_code": "XE"}) == "XE"


def test_ensure_form_line_tax_codes_promotes_header() -> None:
    form = {
        "header": {"tax_code": "V18"},
        "lines": [{"tax_code": ""}, {"tax_code": ""}],
    }
    ensure_form_line_tax_codes(form)
    assert form["lines"][0]["tax_code"] == "V18"
    assert form["lines"][1]["tax_code"] == "V18"
    assert form["header"]["tax_code"] == "V18"


def test_sync_header_tax_code_from_first_line() -> None:
    header: dict[str, str] = {"tax_code": ""}
    lines = [{"tax_code": ""}, {"tax_code": "FB"}]
    sync_header_tax_code(header, lines)
    assert header["tax_code"] == "FB"


def test_yser_po_group_item_tax_code_uses_first_when_all_agree() -> None:
    from app.procurement.line_tax_code import yser_po_group_item_tax_code

    header = {"tax_code": "XE"}
    blocks = [{"tax_code": "FA"}, {"tax_code": "FA"}]
    assert yser_po_group_item_tax_code(blocks, header) == "FA"


def test_yser_po_group_item_tax_code_prefers_last_when_subline_taxes_conflict() -> None:
    from app.procurement.line_tax_code import yser_po_group_item_tax_code

    header = {"tax_code": "FA"}
    blocks = [{"tax_code": "FA"}, {"tax_code": "WA"}]
    assert yser_po_group_item_tax_code(blocks, header) == "WA"


def test_yser_po_group_tax_differs_from_sap_on_second_line_only() -> None:
    from app.procurement.line_tax_code import yser_po_group_tax_differs_from_sap

    header = {"tax_code": "FA"}
    blocks = [{"tax_code": "FA"}, {"tax_code": "WA"}]
    assert yser_po_group_tax_differs_from_sap(blocks, header, sap_tax="FA")
    assert not yser_po_group_tax_differs_from_sap(blocks, header, sap_tax="WA")


def test_po_payload_uses_per_line_tax_codes() -> None:
    blk1 = default_empty_block("YUNB")
    blk1.update(
        {
            "material": "4200000027",
            "short_text": "Line 1",
            "unit_price": "10",
            "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
            "tax_code": "XE",
        }
    )
    blk2 = default_empty_block("YUNB")
    blk2.update(
        {
            "material": "4200000016",
            "short_text": "Line 2",
            "unit_price": "20",
            "allocations": [{"cost_center": "HCO91001A0", "qty": "1"}],
            "tax_code": "FA",
        }
    )
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "material_group": "SD05-0001",
                "vendor": "1000000002",
            },
            "lines": [blk1, blk2],
        },
    )
    payload = build_po_payload(form=form, document_type="YUNB")
    items = payload["to_PurchaseOrderItem"]
    assert len(items) == 2
    assert items[0]["TaxCode"] == "XE"
    assert items[1]["TaxCode"] == "FA"


def test_normalize_form_promotes_header_tax_to_lines() -> None:
    blk = default_empty_block("YUNB")
    blk["material"] = "3000000004"
    blk["allocations"] = [{"cost_center": "HBM11001A0", "qty": "1"}]
    form = normalize_form(
        "YUNB",
        {
            "header": {"tax_code": "FA"},
            "lines": [blk],
        },
    )
    assert form["lines"][0]["tax_code"] == "FA"


def test_normalize_form_preserves_yser_sap_hydration_keys() -> None:
    form = normalize_form(
        "YSER",
        {
            "header": {"purchasing_org": "1MGH"},
            "lines": [
                {
                    "service": "000000001000000000",
                    "service_group": "S089-0001",
                    "purchase_requisition_item": "10",
                    "sap_pr_item": "10",
                    "pr_acct_assgmt_number": "01",
                    "allocations": [
                        {
                            "cost_center": "HBM13021A0",
                            "qty": "1",
                            "pr_acct_assgmt_number": "01",
                            "po_acct_assgmt_number": "0000099210-01-01",
                        },
                        {
                            "cost_center": "HBM13021A1",
                            "qty": "1",
                            "pr_acct_assgmt_number": "02",
                            "po_acct_assgmt_number": "0000099210-01-02",
                        },
                    ],
                }
            ],
        },
    )
    line = form["lines"][0]
    assert line["sap_pr_item"] == "10"
    assert line["pr_acct_assgmt_number"] == "01"
    assert line["allocations"][0]["pr_acct_assgmt_number"] == "01"
    assert line["allocations"][1]["pr_acct_assgmt_number"] == "02"
    assert line["allocations"][0]["po_acct_assgmt_number"] == "0000099210-01-01"
    assert line["allocations"][1]["po_acct_assgmt_number"] == "0000099210-01-02"


def test_old_header_only_po_payload_still_sends_item_tax() -> None:
    """Legacy tickets: header.tax_code only → same item TaxCode as before."""
    blk = default_empty_block("YUNB")
    blk.update(
        {
            "material": "4200000027",
            "unit_price": "10",
            "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
        }
    )
    form = {
        "header": {
            "purchasing_org": "1MGH",
            "purchasing_group": "A0B",
            "plant": "H001",
            "storage_location": "3021",
            "material_group": "SD05-0001",
            "vendor": "1000000002",
            "tax_code": "FA",
        },
        "lines": [blk],
    }
    payload = build_po_payload(form=normalize_form("YUNB", form), document_type="YUNB")
    row = payload["to_PurchaseOrderItem"][0]
    assert row.get("TaxCode") == "FA"


def test_pr_payload_never_includes_tax() -> None:
    from app.procurement.sap_pr_payload import build_pr_payload

    blk = default_empty_block("YUNB")
    blk.update(
        {
            "material": "4200000027",
            "tax_code": "FA",
            "unit_price": "10",
            "allocations": [{"cost_center": "HCO91001H0", "qty": "1"}],
        }
    )
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "material_group": "SD05-0001",
                "tax_code": "FA",
            },
            "lines": [blk],
        },
    )
    payload = build_pr_payload(form=form, document_type="YUNB")
    item = payload["to_PurchaseReqnItem"][0]
    assert "TaxCode" not in item
    assert form["lines"][0]["tax_code"] == "FA"


def test_po_item_patch_keeps_per_line_tax() -> None:
    from app.procurement.sap_po_payload import po_item_patch_body

    body = po_item_patch_body(
        {
            "PurchaseOrder": "4080000001",
            "PurchaseOrderItem": "10",
            "TaxCode": "WA",
            "PurchaseRequisition": "1040000001",
            "PurchaseRequisitionItem": "00010",
        }
    )
    assert body.get("TaxCode") == "WA"
    assert "PurchaseRequisition" not in body


def test_form_from_po_read_maps_item_tax_to_lines() -> None:
    from app.procurement.sap_ticket_form_read import form_from_po_sap_read

    body = {
        "d": {
            "Supplier": "1000000002",
            "PurchasingOrganization": "1MGH",
            "PurchasingGroup": "A0B",
            "to_PurchaseOrderItem": {
                "results": [
                    {
                        "PurchaseOrderItem": "00010",
                        "Material": "4200000027",
                        "TaxCode": "FA",
                        "OrderQuantity": "1",
                        "NetPriceAmount": "10.00",
                        "Plant": "H001",
                        "StorageLocation": "3021",
                        "PurchaseOrderItemText": "Line 1",
                        "PurchaseOrderQuantityUnit": "QT",
                    },
                    {
                        "PurchaseOrderItem": "00020",
                        "Material": "4200000016",
                        "TaxCode": "WA",
                        "OrderQuantity": "2",
                        "NetPriceAmount": "20.00",
                        "Plant": "H001",
                        "StorageLocation": "3021",
                        "PurchaseOrderItemText": "Line 2",
                        "PurchaseOrderQuantityUnit": "QT",
                    },
                ]
            },
        }
    }
    form = form_from_po_sap_read(body, document_type="YUNB")
    lines = form["lines"]
    assert len(lines) == 2
    assert lines[0]["tax_code"] == "FA"
    assert lines[1]["tax_code"] == "WA"
    assert form["header"]["tax_code"] == "FA"
