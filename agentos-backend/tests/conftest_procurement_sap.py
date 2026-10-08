"""Shared procurement SAP test fixtures (avoid importing test modules from tests)."""

from __future__ import annotations

from app.procurement.field_schema import default_empty_block, normalize_form
from app.procurement.sap_defaults import apply_procurement_defaults


def sample_yser_pr_form() -> dict:
    blk = default_empty_block("YSER")
    blk["service"] = "SVC001"
    blk["short_text"] = "Consulting"
    blk["delivery_date"] = "2026-05-15"
    blk["unit_price"] = "50"
    blk["valuation_price"] = "50"
    blk["allocations"] = [{"cost_center": "HBM11001A0", "qty": "2"}]
    form = normalize_form(
        "YSER",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "S0Z",
                "plant": "H002",
                "storage_location": "1001",
                "service_group": "S089-0001",
                "header_note": "Test note",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YSER", kind="PR")
    return form


def sample_yser_po_form(*, vendor: str = "1000000002") -> dict:
    blk = default_empty_block("YSER")
    blk["service"] = "10000000006"
    blk["short_text"] = "Service PO line"
    blk["delivery_date"] = "2026-08-15"
    blk["unit_price"] = "100"
    blk["net_price"] = "100"
    blk["order_unit"] = "EA"
    blk["allocations"] = [{"cost_center": "HCO91004A0", "qty": "2"}]
    form = normalize_form(
        "YSER",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H002",
                "storage_location": "1001",
                "service_group": "S001-0001",
                "vendor": vendor,
                "header_note": "YSER PO test",
                "tax_code": "XE",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YSER", kind="PO")
    return form


def sample_yunb_po_form(*, vendor: str = "1000000002") -> dict:
    blk = default_empty_block("YUNB")
    blk["material"] = "4200000027"
    blk["short_text"] = "Consumable PO"
    blk["unit_price"] = "25"
    blk["net_price"] = "25"
    blk["allocations"] = [{"cost_center": "HCO91001H0", "qty": "2"}]
    form = normalize_form(
        "YUNB",
        {
            "header": {
                "purchasing_org": "1MGH",
                "purchasing_group": "A0B",
                "plant": "H001",
                "storage_location": "3021",
                "material_group": "SD05-0001",
                "vendor": vendor,
                "header_note": "PO integration test",
            },
            "lines": [blk],
        },
    )
    apply_procurement_defaults(form, document_type="YUNB", kind="PO")
    return form
