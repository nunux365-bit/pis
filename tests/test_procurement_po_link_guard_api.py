"""API: prefill-po returns 409 when PR already has a linked PO."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user
from app.api.routes import procurement as procurement_routes
from app.db.models import ProcurementTicket, User, UserRole
from app.db.session import get_db
from app.procurement import service as proc_service
from app.procurement.service import PrAlreadyHasLinkedPoError

app = FastAPI()
app.include_router(procurement_routes.router, prefix="/api/procurement")


@pytest.fixture
def any_user() -> User:
    return User(
        id=uuid.uuid4(),
        email="po-guard@test.example.com",
        roles=[UserRole.EMPLOYEE.value],
        is_active=True,
    )


@pytest.fixture
def client_po_prefill_guard(any_user: User, monkeypatch: pytest.MonkeyPatch):
    pr_id = uuid.uuid4()
    linked_po_id = uuid.uuid4()
    pr = MagicMock(spec=ProcurementTicket)
    pr.id = pr_id
    pr.kind = "PR"
    pr.sap_id = "1010000999"
    pr.document_type = "YSER"

    async def _get_db() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    async def _get_pr_for_po_prefill(session, *, pr_id: uuid.UUID, user_id: uuid.UUID):
        assert pr_id == pr.id
        assert user_id == any_user.id
        return pr

    async def _ensure_pr_available_for_po_link(session, *, parent_pr_id: uuid.UUID) -> None:
        raise PrAlreadyHasLinkedPoError(linked_po_id=linked_po_id)

    monkeypatch.setattr(proc_service, "get_pr_for_po_prefill", _get_pr_for_po_prefill)
    monkeypatch.setattr(proc_service, "ensure_pr_available_for_po_link", _ensure_pr_available_for_po_link)

    app.dependency_overrides[get_current_user] = lambda: any_user
    app.dependency_overrides[get_db] = _get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test"), pr_id, linked_po_id
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_prefill_po_returns_409_with_linked_po_id(client_po_prefill_guard) -> None:
    client, pr_id, linked_po_id = client_po_prefill_guard
    res = await client.get(f"/api/procurement/tickets/{pr_id}/prefill-po")
    assert res.status_code == 409
    body = res.json()
    assert body["detail"]["message"]
    assert "already has a linked purchase order" in body["detail"]["message"].lower()
    assert body["detail"]["linked_po_id"] == str(linked_po_id)
