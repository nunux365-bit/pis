"""Seed rows for pr_po_reference_values (minimal starter master data).

Bootstrap uses ``app/procurement/data/reference_fixed_master.json`` via
``reference_fixed_loader``. This module remains for unit tests only.
``domain`` must match ``reference_domain`` in ``app/procurement/field_schema.py``.
"""

from __future__ import annotations


def pr_po_reference_seed_rows() -> list[dict]:
    rows: list[dict] = []

    def add(
        domain: str,
        doc_type: str,
        code: str,
        label: str,
        sort: int,
        *,
        applies_to_kind: str = "",
    ) -> None:
        rows.append(
            {
                "domain": domain,
                "document_type": doc_type,
                "applies_to_kind": applies_to_kind,
                "code": code,
                "label": label,
                "sort_order": sort,
                "extra": None,
            }
        )

    for i, (c, l) in enumerate(
        [
            ("1MGH", "1mg Healthcare"),
            ("1MGT", "1mg Technologies"),
            ("1LFS", "1mg Lifesciences"),
        ]
    ):
        add("company_code", "", c, l, i)
        add("purchasing_org", "", c, l, i + 10)

    for i, (c, l) in enumerate(
        [
            ("PROC01", "Central Procurement"),
            ("PROC02", "Regional Procurement"),
            ("IT_PROC", "IT Procurement"),
        ]
    ):
        add("purchasing_group", "", c, l, i)

    for i, (c, l) in enumerate(
        [
            ("PL01", "Plant 01 — Karnataka"),
            ("PL02", "Plant 02 — Maharashtra"),
            ("PL03", "Plant 03 — NCR"),
        ]
    ):
        add("plant", "", c, l, i)

    for i, (c, l) in enumerate([("INR", "Indian Rupee"), ("USD", "US Dollar")]):
        add("currency", "", c, l, i)

    for i, (c, l) in enumerate(
        [
            ("V0", "GST 0%"),
            ("V5", "GST 5%"),
            ("V12", "GST 12%"),
            ("V18", "GST 18%"),
        ]
    ):
        add("tax_code", "", c, l, i)

    for i, (c, l) in enumerate([("K", "Cost center"), ("A", "Asset")]):
        add("account_assignment_category", "", c, l, i)

    add("item_category", "", "", "Standard", 0)
    add("item_category", "", "D", "Service", 1)

    for i, (c, l) in enumerate(
        [
            ("MG001", "General services"),
            ("MG002", "IT hardware"),
            ("MG003", "Consumables"),
        ]
    ):
        add("material_group", "", c, l, i)

    for i, (c, l) in enumerate(
        [
            ("SVC001", "Professional services bundle"),
            ("SVC002", "Maintenance contract"),
        ]
    ):
        add("service", "", c, l, i)

    for i, (c, l) in enumerate(
        [
            ("MAT001", "Generic material placeholder"),
            ("MAT002", "Office supplies bundle"),
        ]
    ):
        add("material", "", c, l, i)

    for i, (c, l) in enumerate(
        [
            ("SL01", "Main warehouse"),
            ("SL02", "Secondary store"),
        ]
    ):
        add("storage_location", "", c, l, i)

    for i, (c, l) in enumerate(
        [
            ("CC1000", "Corporate — HQ"),
            ("CC2000", "Operations — North"),
            ("CC3000", "Operations — South"),
        ]
    ):
        add("cost_center", "", c, l, i)

    add("purchasing_doc_type", "", "YSER", "Service PR", 1, applies_to_kind="PR")
    add("purchasing_doc_type", "", "YSER", "Service PO", 2, applies_to_kind="PO")
    add("purchasing_doc_type", "", "YUNB", "PR Non Valuated", 3, applies_to_kind="PR")
    add("purchasing_doc_type", "", "YUNB", "PO Non Valuated", 4, applies_to_kind="PO")

    add("payment_terms", "", "0001", "Immediate", 0)
    add("payment_terms", "", "YI05", "Net 30 (sample)", 1)

    add("order_unit", "", "EA", "Each", 0)
    add("order_unit", "", "PC", "Piece", 1)

    add("vendor", "", "DEMO|1MGH", "Demo Vendor (import Vendor Master.xlsx)", 0, applies_to_kind="")

    return rows
