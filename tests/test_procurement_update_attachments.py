"""Procurement ticket update: attachment total cap (regression guard)."""

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
    u.email = "attach-cap@test.example.com"
    return u


@pytest.mark.asyncio
async def test_update_ticket_rejects_when_total_attachments_would_exceed_cap(
    mock_user: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = uuid.uuid4()
    ticket = MagicMock()
    ticket.id = tid
    ticket.created_by_user_id = mock_user.id
    ticket.version = 1
    ticket.document_type = "YSER"
    ticket.kind = "PR"
    ticket.attachments = [{"id": str(i), "name": f"f{i}.pdf"} for i in range(10)]
    ticket.sap_id = "SAP-1"
    ticket.sap_sync = {"attempt_count": 0, "next_retry_at": None, "last_error": None}

    session = MagicMock()

    # ``update_ticket`` now locks the row via ``SELECT ... FOR UPDATE`` before checking
    # the version, so the test mocks ``execute`` (not ``get``) and shapes the result to
    # look like an ORM scalar fetch.
    fetch_result = MagicMock()
    fetch_result.scalar_one_or_none = MagicMock(return_value=ticket)
    session.execute = AsyncMock(return_value=fetch_result)
    session.add = MagicMock()
    monkeypatch.setattr(proc_service, "write_audit", AsyncMock())

    with pytest.raises(ValueError, match="At most 10 attachments"):
        await proc_service.update_ticket(
            session,
            user=mock_user,
            ticket_id=tid,
            version=1,
            form=None,
            files=[("extra.pdf", "application/pdf", b"%PDF-1.4 minimal")],
            resync_sap=False,
        )
