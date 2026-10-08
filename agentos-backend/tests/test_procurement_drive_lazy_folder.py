"""Procurement: Drive folder is created only when the create request includes attachments."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import User
from app.procurement import service as proc_service


@pytest.fixture
def mock_user() -> MagicMock:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.email = "procurement-drive-test@example.com"
    return u


@pytest.mark.asyncio
async def test_create_ticket_without_files_skips_asyncio_to_thread(mock_user: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """No Drive folder and no thread-pool work when ``files`` is empty (lazy folder on first upload)."""
    monkeypatch.setattr(
        proc_service,
        "normalize_form",
        lambda _dt, form: {"header": dict(form.get("header") or {}), "lines": list(form.get("lines") or [{}])},
    )
    monkeypatch.setattr(proc_service, "validate_form", lambda **_: [])
    monkeypatch.setattr(proc_service, "write_audit", AsyncMock())

    async def forbid_to_thread(*_a: object, **_k: object) -> None:
        raise AssertionError("asyncio.to_thread must not run when creating a ticket with no attachments")

    monkeypatch.setattr(proc_service.asyncio, "to_thread", forbid_to_thread)

    session = MagicMock()
    session.flush = AsyncMock()
    session.add = MagicMock()

    ticket = await proc_service.create_ticket(
        session,
        user=mock_user,
        kind="PR",
        document_type="YSER",
        form={"header": {}, "lines": [{}]},
        parent_pr_id=None,
        files=[],
    )
    assert ticket.drive_folder_id is None
    assert ticket.attachments == []
    assert ticket.sap_sync.get("sync_pending") is True
