"""Attachment sync flags, terminal failure, and attachment-only deferral."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import ProcurementTicket
from app.procurement.attachment_sync import (
    attachment_failed,
    attachment_retryable,
    hidden_sap_attachment_ids,
    merge_attachments_with_sap,
    record_hidden_sap_attachment,
    refresh_attachment_sync_flags,
    reset_attachment_for_retry,
    ticket_has_retryable_attachments,
)
from app.procurement.service import (
    _mark_attachment_sync_deferred,
    _mark_document_sync_deferred,
    _ticket_document_sync_in_flight,
    _ticket_response_skip_sap_hydrate,
)


def _ticket(**kwargs) -> ProcurementTicket:
    base = dict(
        id=uuid.uuid4(),
        kind="PR",
        document_type="YSER",
        form={"header": {}, "lines": []},
        attachments=[],
        sap_id="1010000999",
        sap_sync={},
        version=1,
        created_by_user_id=uuid.uuid4(),
    )
    base.update(kwargs)
    return ProcurementTicket(**base)


def test_public_attachment_row_exposes_can_retry_upload() -> None:
    from app.procurement.attachment_sync import attachment_storage, public_attachment_row

    assert public_attachment_row({"staging_path": "s", "sap_sync_attempt_count": 0})["can_retry_upload"] is True
    assert public_attachment_row({"sap_sync_error": "failed"})["can_retry_upload"] is False
    assert attachment_storage({"staging_path": "s"}) == "local"
    assert attachment_storage({"sap_document_id": "FOL-1"}) == "sap"
    assert public_attachment_row({"sap_document_id": "FOL-1"})["storage"] == "sap"


def test_reconcile_stale_sync_flags_clears_orphan_sync_pending() -> None:
    from app.procurement.service import _reconcile_stale_sync_flags_in_place

    ticket = _ticket(sap_sync={"sync_pending": True, "attachments_pending": False})
    assert _reconcile_stale_sync_flags_in_place(ticket) is True
    assert ticket.sap_sync["sync_pending"] is False
    assert attachment_retryable({"staging_path": "/tmp/x", "sap_sync_attempt_count": 0}) is True
    assert attachment_retryable({"sap_sync_error": "failed"}) is False


def test_terminal_failure_marks_failed_without_staging() -> None:
    row = {
        "name": "a.pdf",
        "staging_path": "s1",
        "sap_sync_attempt_count": 6,
    }
    sync = {"attachments_pending": True}
    ticket = _ticket(attachments=[row])
    refresh_attachment_sync_flags(sync, ticket)
    assert sync["attachments_pending"] is False
    assert attachment_failed(row) is False  # not terminalized until sync runs


def test_refresh_attachment_sync_flags_clears_pending_when_only_failures() -> None:
    row = {"name": "a.pdf", "sap_sync_error": "timeout"}
    sync = {"attachments_pending": True}
    ticket = _ticket(attachments=[row])
    refresh_attachment_sync_flags(sync, ticket)
    assert sync["attachments_pending"] is False
    assert "timeout" in (sync.get("attachment_last_error") or "")


def test_document_sync_in_flight_skips_hydrate_not_attachment_only() -> None:
    ticket = _ticket(
        sap_sync={"sync_pending": True, "attachments_pending": True},
        attachments=[{"id": "1", "staging_path": "s", "sap_sync_attempt_count": 0}],
    )
    assert _ticket_document_sync_in_flight(ticket) is True
    assert _ticket_response_skip_sap_hydrate(ticket) is True

    ticket.sap_sync = {"attachments_pending": True}
    ticket.sap_id = "1010000999"
    assert _ticket_document_sync_in_flight(ticket) is False
    assert _ticket_response_skip_sap_hydrate(ticket) is False


def test_mark_attachment_sync_deferred_does_not_set_sync_pending() -> None:
    ticket = _ticket(sap_sync={"sync_pending": False})
    _mark_attachment_sync_deferred(ticket)
    assert ticket.sap_sync["attachments_pending"] is True
    assert ticket.sap_sync["sync_pending"] is False


def test_mark_document_sync_deferred_sets_sync_pending() -> None:
    ticket = _ticket(sap_sync={})
    _mark_document_sync_deferred(ticket)
    assert ticket.sap_sync["sync_pending"] is True


def test_merge_attachments_preserves_pending_and_adds_sap_only() -> None:
    pending = {
        "id": "local-1",
        "name": "new.pdf",
        "staging_path": "s1",
        "sap_sync_attempt_count": 0,
    }
    synced = {
        "id": "local-2",
        "name": "old.pdf",
        "sap_document_id": "FOL-A",
    }
    sap_rows = [
        {"sap_document_id": "FOL-A", "name": "old.pdf", "mime_type": "application/pdf"},
        {"sap_document_id": "FOL-B", "name": "gui.png", "mime_type": "image/png"},
    ]
    merged = merge_attachments_with_sap([pending, synced], sap_rows)
    assert merged[0] is pending
    assert merged[1]["sap_document_id"] == "FOL-A"
    assert any(r.get("sap_document_id") == "FOL-B" for r in merged)
    assert sum(1 for r in merged if r.get("staging_path")) == 1


def test_merge_attachments_respects_hidden_sap_ids() -> None:
    sap_rows = [{"sap_document_id": "FOL-H", "name": "hidden.pdf", "mime_type": "application/pdf"}]
    merged = merge_attachments_with_sap([], sap_rows, hidden_doc_ids={"FOL-H"})
    assert merged == []


def test_record_hidden_sap_attachment() -> None:
    sync: dict = {}
    record_hidden_sap_attachment(sync, "FOL-1")
    record_hidden_sap_attachment(sync, "FOL-1")
    assert hidden_sap_attachment_ids(sync) == {"FOL-1"}


def test_reset_attachment_for_retry_clears_error() -> None:
    row = {"sap_sync_error": "x", "sap_sync_attempt_count": 3, "sap_next_retry_at": "t"}
    reset_attachment_for_retry(row)
    assert row["sap_sync_error"] is None
    assert row["sap_sync_attempt_count"] == 0
    assert row["sap_next_retry_at"] is None


@pytest.mark.asyncio
async def test_sync_pending_attachments_terminalizes_exhausted() -> None:
    from app.procurement.attachment_sync import sync_pending_attachments_for_ticket

    row = {
        "name": "a.pdf",
        "staging_path": "s1",
        "sap_sync_attempt_count": 6,
        "mime_type": "application/pdf",
    }
    ticket = _ticket(attachments=[row])
    with patch(
        "app.procurement.attachment_sync.sap_attachment_configured",
        return_value=True,
    ):
        ok, fail, err = await sync_pending_attachments_for_ticket(ticket, ticket_id=str(ticket.id))
    assert ok == 0
    assert fail == 1
    assert "exhausted" in (err or "")
    assert row.get("staging_path") is None
    assert row.get("sap_sync_error")


@pytest.mark.asyncio
async def test_hydrate_attachments_from_sap_merges_rows() -> None:
    from app.procurement.attachment_sync import hydrate_attachments_from_sap

    ticket = _ticket(
        attachments=[{"id": "a1", "name": "x.pdf", "sap_document_id": "FOL-A"}],
    )
    sap_rows = [
        {"sap_document_id": "FOL-A", "name": "x.pdf", "mime_type": "application/pdf"},
        {"sap_document_id": "FOL-B", "name": "sap-only.pdf", "mime_type": "application/pdf"},
    ]
    with patch(
        "app.procurement.attachment_sync.list_attachments",
        new=AsyncMock(return_value=(sap_rows, None)),
    ):
        with patch(
            "app.procurement.attachment_sync.sap_attachment_configured",
            return_value=True,
        ):
            err = await hydrate_attachments_from_sap(ticket)
    assert err is None
    ids = {a.get("sap_document_id") for a in ticket.attachments if isinstance(a, dict)}
    assert ids == {"FOL-A", "FOL-B"}


@pytest.mark.asyncio
async def test_get_ticket_sync_status_skips_form_hydrate() -> None:
    from app.procurement.service import get_ticket_sync_status

    ticket = _ticket()
    session = AsyncMock()
    session.get = AsyncMock(return_value=ticket)
    session.add = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    with patch(
        "app.procurement.service._enrich_ticket_on_read",
        new=AsyncMock(return_value=ticket),
    ) as enrich:
        out = await get_ticket_sync_status(
            session, ticket_id=ticket.id, user_id=ticket.created_by_user_id
        )
    assert out is ticket
    enrich.assert_awaited_once()
    assert enrich.await_args.kwargs == {"hydrate_form": False, "hydrate_attachments": True}


@pytest.mark.asyncio
async def test_update_ticket_attachment_only_sets_attachments_pending() -> None:
    from app.db.models import User
    from app.procurement.service import update_ticket

    user = User(id=uuid.uuid4(), email="u@example.com")
    ticket = _ticket(
        created_by_user_id=user.id,
        sap_sync={"sync_pending": False},
        attachments=[],
    )
    result = AsyncMock()
    result.scalar_one_or_none = lambda: ticket
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.add = AsyncMock()
    with patch("app.procurement.service.write_audit", new=AsyncMock()):
        with patch("app.procurement.service._ticket_for_write_response", new=AsyncMock(return_value=ticket)):
            with patch(
                "app.procurement.service.stage_uploaded_files",
                return_value=[{"id": "a1", "name": "x.pdf", "staging_path": "s", "sap_sync_attempt_count": 0}],
            ):
                out = await update_ticket(
                    session,
                    user=user,
                    ticket_id=ticket.id,
                    version=1,
                    form=None,
                    files=[("x.pdf", "application/pdf", b"%PDF")],
                    resync_sap=False,
                )
    assert out.sap_sync["attachments_pending"] is True
    assert out.sap_sync.get("sync_pending") is not True
    assert ticket_has_retryable_attachments(ticket)


@pytest.mark.asyncio
async def test_delete_attachment_rejects_sap_files() -> None:
    from app.db.models import User
    from app.procurement.service import delete_attachment

    user = User(id=uuid.uuid4(), email="u@example.com")
    ticket = _ticket(
        created_by_user_id=user.id,
        attachments=[{"id": "a1", "name": "x.pdf", "sap_document_id": "FOL-99"}],
    )
    result = AsyncMock()
    result.scalar_one_or_none = lambda: ticket
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    with pytest.raises(ValueError, match="sap_delete_not_available"):
        await delete_attachment(
            session, user=user, ticket_id=ticket.id, attachment_id="a1"
        )
