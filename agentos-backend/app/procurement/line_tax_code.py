"""Per-line tax code (with legacy header fallback)."""

from __future__ import annotations

from typing import Any


def _norm(v: Any) -> str:
    return str(v or "").strip()


def line_tax_code(block: dict[str, Any], header: dict[str, Any]) -> str:
    """Line tax wins; fall back to header for legacy single-tax forms."""
    line_val = _norm(block.get("tax_code"))
    if line_val:
        return line_val
    return _norm(header.get("tax_code"))


def yser_po_group_line_taxes(
    group_blocks: list[dict[str, Any]], header: dict[str, Any]
) -> list[str]:
    """Effective tax code per UI service line in one grouped SAP PO item."""
    out: list[str] = []
    for block in group_blocks:
        if not isinstance(block, dict):
            continue
        tc = line_tax_code(block, header)
        if tc:
            out.append(tc)
    return out


def yser_po_group_item_tax_code(
    group_blocks: list[dict[str, Any]],
    header: dict[str, Any],
    *,
    sap_tax: str = "",
) -> str:
    """Canonical ``TaxCode`` for one SAP PO item that covers multiple UI service lines."""
    taxes = yser_po_group_line_taxes(group_blocks, header)
    if not taxes:
        return _norm(header.get("tax_code"))
    unique = list(dict.fromkeys(taxes))
    if len(unique) == 1:
        return unique[0]
    # SAP stores one code per item; when UI sublines disagree, last line wins.
    return taxes[-1]


def yser_po_group_tax_differs_from_sap(
    group_blocks: list[dict[str, Any]],
    header: dict[str, Any],
    *,
    sap_tax: str,
) -> bool:
    """True when the resolved grouped item tax disagrees with SAP."""
    sap_tax = _norm(sap_tax)
    if not sap_tax:
        return False
    desired = yser_po_group_item_tax_code(group_blocks, header)
    return bool(desired and desired != sap_tax)


def sync_header_tax_code(header: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    """Keep header tax aligned with the first line that has a code (DB / API compat)."""
    for row in lines:
        if not isinstance(row, dict):
            continue
        tc = _norm(row.get("tax_code"))
        if tc:
            header["tax_code"] = tc
            return
    if not _norm(header.get("tax_code")):
        header["tax_code"] = ""


def ensure_form_line_tax_codes(form: dict[str, Any]) -> None:
    """Promote legacy header-only tax onto lines; sync header from lines."""
    header = form.get("header")
    if not isinstance(header, dict):
        return
    lines = form.get("lines")
    if not isinstance(lines, list):
        return
    header_tax = _norm(header.get("tax_code"))
    for row in lines:
        if not isinstance(row, dict):
            continue
        if not _norm(row.get("tax_code")) and header_tax:
            row["tax_code"] = header_tax
    sync_header_tax_code(header, lines)
