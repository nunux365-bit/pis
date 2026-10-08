"""YAST material PR/PO account assignment — category ``A`` (asset), shared mapping."""

from __future__ import annotations

from typing import Any

from app.procurement.sap_odata_utils import (
    odata_entity_properties,
    odata_norm as _norm,
    odata_results_list,
    odata_text,
)

ACCOUNT_ASSIGNMENT_COST_CENTER = "K"
ACCOUNT_ASSIGNMENT_ASSET = "A"

def material_account_assignment_category(document_type: str, form_value: str) -> str:
    """YUNB → ``K``; YAST → ``A`` (asset). Explicit form value wins."""
    explicit = _norm(form_value)
    if explicit:
        return explicit
    dt = (document_type or "").upper()
    if dt == "YAST":
        return ACCOUNT_ASSIGNMENT_ASSET
    if dt == "YUNB":
        return ACCOUNT_ASSIGNMENT_COST_CENTER
    return ""


def yast_acct_row_should_emit(*, asset: str, qty: str) -> bool:
    """Emit when the allocation has an asset and quantity (cost centre is unused for YAST)."""
    return bool(_norm(asset) and _norm(qty))


def asset_codes_equal(left: str, right: str) -> bool:
    """Compare SAP / catalogue asset numbers without leading-zero pad sensitivity."""
    a, b = _norm(left), _norm(right)
    if a == b:
        return True
    return (a.lstrip("0") or "0") == (b.lstrip("0") or "0")


def yast_acct_extra_fields(*, asset: str) -> dict[str, str]:
    """Only asset on outbound SAP rows — GL / controlling area left to SAP derivation."""
    asset = _norm(asset)
    if not asset:
        return {}
    return {"MasterFixedAsset": asset}


def yast_optional_gl_fields(
    *,
    header: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Include GL / CO only when explicitly present on the form (no backend defaults)."""
    out: dict[str, str] = {}
    h = header if isinstance(header, dict) else {}
    b = block if isinstance(block, dict) else {}
    gl = _norm(h.get("gl_account")) or _norm(b.get("gl_account"))
    co = _norm(h.get("controlling_area")) or _norm(b.get("controlling_area"))
    if gl:
        out["GLAccount"] = gl
    if co:
        out["ControllingArea"] = co
    return out


def allocations_from_sap_acct_node(
    acct_node: Any,
    *,
    document_type: str,
    order_qty_fallback: str = "",
) -> tuple[list[dict[str, str]], str]:
    """Map OData acct segments → UI allocations + line-level asset (first MasterFixedAsset)."""
    dt = (document_type or "").upper()
    rows: list[dict[str, str]] = []
    first_asset = ""
    for acct_entry in odata_results_list(acct_node):
        ap = odata_entity_properties(acct_entry)
        ma = odata_text(ap.get("MasterFixedAsset"))
        cc = odata_text(ap.get("CostCenter"))
        qty = odata_text(ap.get("Quantity")) or "1"
        if dt == "YAST":
            if ma or qty:
                rows.append({"asset": ma, "qty": qty})
                if not first_asset and ma:
                    first_asset = ma
        elif cc:
            rows.append({"cost_center": cc, "qty": qty})
    if not rows and order_qty_fallback:
        if dt == "YAST":
            rows.append({"asset": "", "qty": order_qty_fallback})
        else:
            rows.append({"cost_center": "", "qty": order_qty_fallback})
    return rows, first_asset
