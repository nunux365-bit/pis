"""API: attachment retry endpoint."""

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

app = FastAPI()
app.include_router(procurement_routes.router, prefix="/api/procurement")


@pytest.fixture
def any_user() -> User:
    return User(
        id=uuid.uuid4(),
        email="att-retry@test.example.com",
        roles=[UserRole.EMPLOYEE.value],
        is_active=True,
    )


@pytest.fixture
def client_attachment_retry(any_user: User, monkeypatch: pytest.MonkeyPatch):
    ticket_id = uuid.uuid4()
    attachment_id = "att-1"
    ticket = MagicMock(spec=ProcurementTicket)
    ticket.id = ticket_id

    async def _get_db() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    async def _retry_ok(db, *, user, ticket_id, attachment_id):
        assert user.id == any_user.id
        return ticket

    async def _retry_err(db, *, user, ticket_id, attachment_id):
        raise ValueError("reattach_required")

    async def _to_out_enriched(db, t):
        from datetime import UTC, datetime

        from app.schemas.procurement import ProcurementTicketOut

        return ProcurementTicketOut(
            id=ticket_id,
            kind="PR",
            parent_pr_id=None,
            document_type="YSER",
            form={"header": {}, "lines": []},
            attachments=[],
            drive_folder_id=None,
            sap_id="1010000999",
            sap_sync={},
            version=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(proc_service, "retry_attachment_upload", _retry_ok)
    monkeypatch.setattr(procurement_routes, "_to_out_enriched", _to_out_enriched)
    scheduled: list[dict] = []

    def _schedule(**kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(proc_service, "schedule_procurement_sap_work", _schedule)

    app.dependency_overrides[get_current_user] = lambda: any_user
    app.dependency_overrides[get_db] = _get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test"), ticket_id, attachment_id, scheduled
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_retry_attachment_success_schedules_background_work(client_attachment_retry) -> None:
    client, ticket_id, attachment_id, scheduled = client_attachment_retry
    res = await client.post(f"/api/procurement/tickets/{ticket_id}/attachments/{attachment_id}/retry")
    assert res.status_code == 200
    assert scheduled and scheduled[0]["sync_attachments"] is True


@pytest.mark.asyncio
async def test_retry_attachment_reattach_required_returns_400(
    any_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_id = uuid.uuid4()

    async def _get_db() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    async def _retry_err(db, *, user, ticket_id, attachment_id):
        raise ValueError("reattach_required")

    monkeypatch.setattr(proc_service, "retry_attachment_upload", _retry_err)
    app.dependency_overrides[get_current_user] = lambda: any_user
    app.dependency_overrides[get_db] = _get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(f"/api/procurement/tickets/{ticket_id}/attachments/x/retry")
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 400
    assert "attach it again" in res.json()["detail"].lower()
