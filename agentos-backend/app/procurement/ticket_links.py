"""PR↔PO link summaries for ticket list/detail (DB only — no SAP calls)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcurementTicket


def _summary_row(t: ProcurementTicket) -> dict[str, Any]:
    return {
        "id": t.id,
        "kind": str(t.kind or ""),
        "sap_id": (t.sap_id or "").strip() or None,
        "document_type": str(t.document_type or ""),
    }


async def link_fields_for_ticket(
    session: AsyncSession, ticket: ProcurementTicket
) -> dict[str, Any]:
    """``parent_pr_summary`` / ``linked_pos`` for a single ticket row."""
    out: dict[str, Any] = {"parent_pr_summary": None, "linked_pos": []}
    kind = str(ticket.kind or "").upper()
    if kind == "PO" and ticket.parent_pr_id:
        parent = await session.get(ProcurementTicket, ticket.parent_pr_id)
        if parent is not None:
            out["parent_pr_summary"] = _summary_row(parent)
    if kind == "PR":
        res = await session.execute(
            select(ProcurementTicket)
            .where(ProcurementTicket.parent_pr_id == ticket.id)
            .order_by(ProcurementTicket.updated_at.desc())
        )
        out["linked_pos"] = [_summary_row(row) for row in res.scalars().all()]
    return out


async def link_fields_for_tickets(
    session: AsyncSession, tickets: list[ProcurementTicket]
) -> dict[UUID, dict[str, Any]]:
    """Batch link enrichment for list responses."""
    if not tickets:
        return {}
    by_id = {t.id: t for t in tickets}
    result: dict[UUID, dict[str, Any]] = {
        tid: {"parent_pr_summary": None, "linked_pos": []} for tid in by_id
    }

    pr_ids = [t.id for t in tickets if str(t.kind or "").upper() == "PR"]
    parent_ids = {
        t.parent_pr_id
        for t in tickets
        if str(t.kind or "").upper() == "PO" and t.parent_pr_id is not None
    }

    if parent_ids:
        res = await session.execute(
            select(ProcurementTicket).where(ProcurementTicket.id.in_(parent_ids))
        )
        parents = {p.id: p for p in res.scalars().all()}
        for t in tickets:
            if t.parent_pr_id and t.parent_pr_id in parents:
                result[t.id]["parent_pr_summary"] = _summary_row(parents[t.parent_pr_id])

    if pr_ids:
        res = await session.execute(
            select(ProcurementTicket)
            .where(ProcurementTicket.parent_pr_id.in_(pr_ids))
            .order_by(ProcurementTicket.updated_at.desc())
        )
        for child in res.scalars().all():
            pid = child.parent_pr_id
            if pid is not None and pid in result:
                result[pid]["linked_pos"].append(_summary_row(child))

    return result
