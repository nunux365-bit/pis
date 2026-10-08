"""SAP-facing defaults (Phase 1) — CoCd, account assignment, item category."""

from __future__ import annotations

from typing import Any

# SAP PR API samples (PR API-Structure / PR All) use empty account assignment on create.
DEFAULT_ACCOUNT_ASSIGNMENT_CATEGORY = ""
# Z_PURCHASE_REQUISITION_SRV Service PR doc uses ItemCat ``D``.
ITEM_CATEGORY_SERVICE = "D"
ITEM_CATEGORY_STANDARD = ""

# Default order / PO unit for material document types when the client leaves it blank.
DEFAULT_ORDER_UNIT = "QT"


def apply_procurement_defaults(form: dict[str, Any], *, document_type: str, kind: str) -> None:
    """Mutate normalized ``form`` in place: header CoCd, hidden SAP fields, price mirrors."""
    dt = (document_type or "").upper()
    kind_u = (kind or "").upper()
    header = form.get("header")
    if not isinstance(header, dict):
        return
    org = str(header.get("purchasing_org") or "").strip()
    if org:
        header["company_code"] = org
    # ``purchasing_doc_type`` is not sent on OData create (workflow type is YSER/YUNB/YAST).
    # PO payment terms: omit on SAP unless set on form (no UI yet); do not clear user/PR-prefill values.
    if kind_u != "PO":
        header["payment_terms"] = ""

    lines = form.get("lines")
    if not isinstance(lines, list):
        return

    from app.procurement.sap_odata_utils import sanitize_form_delivery_date

    for row in lines:
        if not isinstance(row, dict):
            continue
        # Drop epoch/invalid dates only — do not synthesize delivery here (PR enrichment
        # and SAP payload build apply PR dates or posting-time defaults).
        row["delivery_date"] = sanitize_form_delivery_date(str(row.get("delivery_date") or ""))
        row["account_assignment_cat"] = DEFAULT_ACCOUNT_ASSIGNMENT_CATEGORY
        if dt == "YSER":
            row["item_category"] = ITEM_CATEGORY_SERVICE
        else:
            row["item_category"] = ITEM_CATEGORY_STANDARD

        if dt in ("YSER", "YUNB", "YAST") and not str(row.get("order_unit") or "").strip():
            row["order_unit"] = "EA" if dt == "YSER" else DEFAULT_ORDER_UNIT

        up = str(row.get("unit_price") or "").strip()
        if up:
            if kind_u == "PO":
                row["net_price"] = up
            if dt == "YSER" or kind_u == "PR":
                # Hydrated YSER lines store line-total in ``gross_price`` (same as
                # ``valuation_price``). Do not overwrite with unit price — that makes
                # every sibling look changed on PATCH resync and breaks Z updates.
                if not str(row.get("gross_price") or "").strip():
                    row["gross_price"] = up
