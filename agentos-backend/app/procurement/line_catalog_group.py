"""Per-line material / service catalogue group (with legacy header fallback)."""

from __future__ import annotations

from typing import Any


def _norm(v: Any) -> str:
    return str(v or "").strip()


def catalog_group_field(document_type: str) -> str:
    dt = (document_type or "").upper()
    return "service_group" if dt == "YSER" else "material_group"


def line_catalog_group(
    block: dict[str, Any],
    header: dict[str, Any],
    document_type: str,
) -> str:
    """Line group wins; fall back to header for legacy single-group forms."""
    key = catalog_group_field(document_type)
    block_val = _norm(block.get(key))
    if block_val:
        return block_val
    return _norm(header.get(key))


def sync_header_catalog_group(
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    document_type: str,
) -> None:
    """Keep header group aligned with the first line (backward compat / DB shape)."""
    key = catalog_group_field(document_type)
    for row in lines:
        if not isinstance(row, dict):
            continue
        grp = _norm(row.get(key))
        if grp:
            header[key] = grp
            return
    if not _norm(header.get(key)):
        header[key] = ""


def ensure_form_line_catalog_groups(form: dict[str, Any], document_type: str) -> None:
    """Promote legacy header-only group onto lines; sync header from lines."""
    dt = (document_type or "").upper()
    header = form.get("header")
    if not isinstance(header, dict):
        return
    lines = form.get("lines")
    if not isinstance(lines, list):
        return
    key = catalog_group_field(dt)
    header_grp = _norm(header.get(key))
    for row in lines:
        if not isinstance(row, dict):
            continue
        if not _norm(row.get(key)) and header_grp:
            row[key] = header_grp
    sync_header_catalog_group(header, lines, dt)
