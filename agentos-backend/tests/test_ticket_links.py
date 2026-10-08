"""PR↔PO link enrichment on ticket list/detail payloads."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import ProcurementTicket
from app.procurement.ticket_links import link_fields_for_ticket, link_fields_for_tickets


def _ticket(
    *,
    kind: str,
    sap_id: str | None = None,
    parent_pr_id: uuid.UUID | None = None,
    document_type: str = "YSER",
) -> ProcurementTicket:
    t = MagicMock(spec=ProcurementTicket)
    t.id = uuid.uuid4()
    t.kind = kind
    t.sap_id = sap_id
    t.parent_pr_id = parent_pr_id
    t.document_type = document_type
    return t


@pytest.mark.asyncio
async def test_link_fields_po_includes_parent_pr_summary() -> None:
    pr_id = uuid.uuid4()
    pr = _ticket(kind="PR", sap_id="PR-100", document_type="YSER")
    pr.id = pr_id
    po = _ticket(kind="PO", sap_id="PO-200", parent_pr_id=pr_id)

    session = MagicMock()
    session.get = AsyncMock(return_value=pr)

    out = await link_fields_for_ticket(session, po)
    assert out["parent_pr_summary"] == {
        "id": pr_id,
        "kind": "PR",
        "sap_id": "PR-100",
        "document_type": "YSER",
    }
    assert out["linked_pos"] == []


@pytest.mark.asyncio
async def test_link_fields_pr_lists_child_pos() -> None:
    pr_id = uuid.uuid4()
    pr = _ticket(kind="PR", sap_id="PR-100")
    pr.id = pr_id
    po1 = _ticket(kind="PO", sap_id="PO-1", parent_pr_id=pr_id)
    po2 = _ticket(kind="PO", sap_id="PO-2", parent_pr_id=pr_id)

    class _Scalars:
        def __init__(self, rows: list[ProcurementTicket]) -> None:
            self._rows = rows

        def all(self) -> list[ProcurementTicket]:
            return self._rows

    class _Result:
        def __init__(self, rows: list[ProcurementTicket]) -> None:
            self._rows = rows

        def scalars(self) -> _Scalars:
            return _Scalars(self._rows)

    session = MagicMock()
    session.execute = AsyncMock(return_value=_Result([po1, po2]))

    out = await link_fields_for_ticket(session, pr)
    assert out["parent_pr_summary"] is None
    assert len(out["linked_pos"]) == 2
    assert {x["sap_id"] for x in out["linked_pos"]} == {"PO-1", "PO-2"}


@pytest.mark.asyncio
async def test_link_fields_batch_merges_parents_and_children() -> None:
    pr_id = uuid.uuid4()
    pr = _ticket(kind="PR", sap_id="PR-9")
    pr.id = pr_id
    po = _ticket(kind="PO", sap_id="PO-9", parent_pr_id=pr_id)
    child = _ticket(kind="PO", sap_id="PO-CHILD", parent_pr_id=pr_id)

    parents = {pr_id: pr}
    children = [child]

    async def _execute(stmt):  # noqa: ANN001
        sql = str(stmt)
        if "parent_pr_id IN" in sql or "parent_pr_id.in_" in sql.lower():
            return MagicMock(scalars=lambda: MagicMock(all=lambda: children))
        return MagicMock(scalars=lambda: MagicMock(all=lambda: list(parents.values())))

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    out = await link_fields_for_tickets(session, [pr, po])
    assert out[pr.id]["linked_pos"][0]["sap_id"] == "PO-CHILD"
    assert out[po.id]["parent_pr_summary"]["sap_id"] == "PR-9"
