"""Normalized rows for ``pr_po_reference_values`` upsert."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReferenceRow:
    domain: str
    code: str
    label: str
    sort_order: int = 0
    document_type: str = ""
    applies_to_kind: str = ""
    extra: dict[str, Any] | None = None


def dedupe_reference_rows(rows: list[ReferenceRow]) -> list[ReferenceRow]:
    """Last row wins per natural key (SAP may repeat codes in one OData page)."""
    out: dict[tuple[str, str, str, str], ReferenceRow] = {}
    for row in rows:
        if not row.code or not row.domain:
            continue
        key = (row.domain, row.document_type, row.code, row.applies_to_kind)
        out[key] = row
    return list(out.values())


def merge_extra(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge JSONB ``extra`` — keep existing keys when SAP sends blanks (HANA facets)."""
    if not existing and not incoming:
        return None
    if not existing:
        return dict(incoming) if incoming else None
    if not incoming:
        return dict(existing)
    out = dict(existing)
    for key, val in incoming.items():
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        out[key] = val
    return out
