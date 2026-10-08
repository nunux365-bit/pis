"""Resolve line ``order_unit`` from material/service reference master (not shown in UI)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PrPoReferenceValue
from app.procurement.reference_query import reference_list_filter
from app.procurement.sap_defaults import DEFAULT_ORDER_UNIT
from app.procurement.sap_pr_payload import order_unit_from_reference_extra

YSER_DEFAULT_ORDER_UNIT = "EA"


def _line_catalog_code(document_type: str, row: dict[str, Any]) -> tuple[str, str] | None:
    dt = (document_type or "").upper()
    if dt == "YSER":
        code = str(row.get("service") or "").strip()
        return ("service", code) if code else None
    if dt in ("YUNB", "YAST"):
        code = str(row.get("material") or "").strip()
        return ("material", code) if code else None
    return None


def _pick_reference_extra(
    rows: list[tuple[str, str, str, Any]],
    *,
    code: str,
    workflow_dt: str,
    ticket_kind: str,
) -> Any | None:
    """Prefer workflow-specific row over generic (``document_type`` / ``applies_to_kind`` empty)."""
    candidates = [r for r in rows if r[0] == code]
    if not candidates:
        return None
    kind_u = (ticket_kind or "PR").upper()

    def _score(row: tuple[str, str, str, Any]) -> int:
        _code, ref_dt, ref_kind, _extra = row
        s = 0
        if ref_dt == workflow_dt:
            s += 8
        elif ref_dt == "":
            s += 4
        else:
            s -= 16
        if ref_kind == kind_u:
            s += 2
        elif ref_kind == "":
            s += 1
        return s

    return max(candidates, key=_score)[3]


async def apply_line_order_units_from_reference(
    session: AsyncSession,
    *,
    form: dict[str, Any],
    document_type: str,
    ticket_kind: str = "PR",
) -> None:
    """Mutate ``form`` lines: ``order_unit`` from catalogue ``extra``, else workflow default."""
    dt = (document_type or "").upper()
    kind_u = (ticket_kind or "PR").upper()
    fallback_uom = YSER_DEFAULT_ORDER_UNIT if dt == "YSER" else DEFAULT_ORDER_UNIT
    lines = form.get("lines")
    if not isinstance(lines, list):
        return

    codes_by_domain: dict[str, list[str]] = {}
    for row in lines:
        if not isinstance(row, dict):
            continue
        hit = _line_catalog_code(dt, row)
        if hit:
            domain, code = hit
            codes_by_domain.setdefault(domain, [])
            if code not in codes_by_domain[domain]:
                codes_by_domain[domain].append(code)

    if not codes_by_domain:
        return

    extra_by_domain_code: dict[tuple[str, str], Any] = {}
    for domain, codes in codes_by_domain.items():
        res = await session.execute(
            select(
                PrPoReferenceValue.code,
                PrPoReferenceValue.document_type,
                PrPoReferenceValue.applies_to_kind,
                PrPoReferenceValue.extra,
            ).where(
                PrPoReferenceValue.domain == domain,
                PrPoReferenceValue.code.in_(codes),
                reference_list_filter(
                    domain=domain, workflow_document_type=dt, ticket_kind=kind_u
                ),
            )
        )
        rows = [(str(c), str(ref_dt), str(ref_kind), extra) for c, ref_dt, ref_kind, extra in res.all()]
        for code in codes:
            picked = _pick_reference_extra(rows, code=code, workflow_dt=dt, ticket_kind=kind_u)
            if picked is not None:
                extra_by_domain_code[(domain, code)] = picked

    for row in lines:
        if not isinstance(row, dict):
            continue
        hit = _line_catalog_code(dt, row)
        if not hit:
            if dt in ("YSER", "YUNB", "YAST") and not str(row.get("order_unit") or "").strip():
                row["order_unit"] = fallback_uom
            continue
        domain, code = hit
        extra = extra_by_domain_code.get((domain, code))
        unit = order_unit_from_reference_extra(extra) or fallback_uom
        row["order_unit"] = unit
