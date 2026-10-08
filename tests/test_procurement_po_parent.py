"""Procurement PO creation: optional ``parent_pr_id`` and parent PR validation."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import User
from app.procurement import service as proc_service
from app.procurement.service import PrAlreadyHasLinkedPoError
from app.schemas.procurement import ProcurementPoCreateFormPayload


@pytest.fixture
def mock_user() -> MagicMock:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.email = "procurement-test@example.com"
    return u


@pytest.fixture
def stub_form_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        proc_service,
        "normalize_form",
        lambda _dt, form: {"header": dict(form.get("header") or {}), "lines": list(form.get("lines") or [{"x": "1"}])},
    )
    monkeypatch.setattr(proc_service, "validate_form", lambda **_: [])
    monkeypatch.setattr(proc_service, "_run_sap_sync", AsyncMock())
    monkeypatch.setattr(proc_service, "write_audit", AsyncMock())

def _session_for_create_ticket(*, get_return: Any = None) -> MagicMock:
    s = MagicMock()
    s.get = AsyncMock(return_value=get_return)
    s.flush = AsyncMock()
    s.add = MagicMock()
    empty = MagicMock()
    empty.scalar_one_or_none.return_value = None
    s.execute = AsyncMock(return_value=empty)
    return s


async def test_po_create_defaults_requestor_email_when_empty(
    mock_user: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _add(ticket: Any) -> None:
        captured["requestor_email"] = (ticket.form or {}).get("header", {}).get("requestor_email")

    monkeypatch.setattr(proc_service, "normalize_form", lambda _dt, form: {
        "header": dict(form.get("header") or {}),
        "lines": list(form.get("lines") or [{}]),
    })
    monkeypatch.setattr(proc_service, "validate_form", lambda **_: [])
    monkeypatch.setattr(proc_service, "apply_line_order_units_from_reference", AsyncMock())
    monkeypatch.setattr(proc_service, "apply_vendor_payment_terms_from_catalogue", AsyncMock())
    monkeypatch.setattr(proc_service, "validate_form_against_catalogue", AsyncMock(return_value=[]))
    monkeypatch.setattr(proc_service, "write_audit", AsyncMock())
    session = _session_for_create_ticket()
    session.add = MagicMock(side_effect=_add)
    await proc_service.create_ticket(
        session,
        user=mock_user,
        kind="PO",
        document_type="YUNB",
        form={"header": {}, "lines": [{}]},
        parent_pr_id=None,
        files=[],
    )
    assert captured["requestor_email"] == mock_user.email


async def test_po_create_preserves_client_requestor_email(
    mock_user: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _add(ticket: Any) -> None:
        captured["requestor_email"] = (ticket.form or {}).get("header", {}).get("requestor_email")

    monkeypatch.setattr(proc_service, "normalize_form", lambda _dt, form: {
        "header": dict(form.get("header") or {}),
        "lines": list(form.get("lines") or [{}]),
    })
    monkeypatch.setattr(proc_service, "validate_form", lambda **_: [])
    monkeypatch.setattr(proc_service, "apply_line_order_units_from_reference", AsyncMock())
    monkeypatch.setattr(proc_service, "apply_vendor_payment_terms_from_catalogue", AsyncMock())
    monkeypatch.setattr(proc_service, "validate_form_against_catalogue", AsyncMock(return_value=[]))
    monkeypatch.setattr(proc_service, "write_audit", AsyncMock())
    session = _session_for_create_ticket()
    session.add = MagicMock(side_effect=_add)
    await proc_service.create_ticket(
        session,
        user=mock_user,
        kind="PO",
        document_type="YUNB",
        form={"header": {"requestor_email": "client@example.com"}, "lines": [{}]},
        parent_pr_id=None,
        files=[],
    )
    assert captured["requestor_email"] == "client@example.com"


async def test_po_creator_email_resolves_from_ticket_creator_not_sync_actor() -> None:
    creator = MagicMock(spec=User)
    creator.email = "creator@1mg.com"
    session = MagicMock()
    session.get = AsyncMock(return_value=creator)
    ticket = MagicMock(
        kind="PO",
        created_by_user_id=uuid.uuid4(),
    )
    email = await proc_service._po_creator_email_for_ticket(session, ticket)
    assert email == "creator@1mg.com"
    session.get.assert_awaited_once_with(User, ticket.created_by_user_id)


async def test_po_standalone_does_not_query_parent_pr(mock_user: MagicMock, stub_form_pipeline: None) -> None:
    session = _session_for_create_ticket()
    ticket = await proc_service.create_ticket(
        session,
        user=mock_user,
        kind="PO",
        document_type="YSER",
        form={"header": {}, "lines": [{}]},
        parent_pr_id=None,
        files=[],
    )
    session.get.assert_not_awaited()
    assert ticket.parent_pr_id is None
    assert ticket.kind == "PO"


async def test_po_parent_wrong_kind_raises(mock_user: MagicMock, stub_form_pipeline: None) -> None:
    parent_id = uuid.uuid4()
    bogus = MagicMock(kind="PO", sap_id="MOCK-PR-X", created_by_user_id=mock_user.id, document_type="YSER")
    session = _session_for_create_ticket(get_return=bogus)
    with pytest.raises(ValueError, match="Invalid parent PR"):
        await proc_service.create_ticket(
            session,
            user=mock_user,
            kind="PO",
            document_type="YSER",
            form={"header": {}, "lines": [{}]},
            parent_pr_id=parent_id,
            files=[],
        )
    session.get.assert_awaited()


async def test_po_parent_pr_missing_sap_id_raises(mock_user: MagicMock, stub_form_pipeline: None) -> None:
    parent_id = uuid.uuid4()
    pr = MagicMock(kind="PR", sap_id=None, created_by_user_id=mock_user.id, document_type="YSER")
    session = _session_for_create_ticket(get_return=pr)
    with pytest.raises(ValueError, match="SAP id"):
        await proc_service.create_ticket(
            session,
            user=mock_user,
            kind="PO",
            document_type="YSER",
            form={"header": {}, "lines": [{}]},
            parent_pr_id=parent_id,
            files=[],
        )


async def test_po_with_sap_backed_parent_ok(
    mock_user: MagicMock, stub_form_pipeline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_id = uuid.uuid4()
    pr = MagicMock(kind="PR", sap_id="MOCK-PR-OK", created_by_user_id=mock_user.id, document_type="YSER")
    session = _session_for_create_ticket(get_return=pr)

    async def _enrich(form, **kwargs):
        return None

    monkeypatch.setattr(proc_service, "enrich_po_form_from_sap_pr", _enrich)
    monkeypatch.setattr(
        proc_service,
        "_validate_po_allocations_against_parent_pr",
        AsyncMock(return_value=None),
    )
    ticket = await proc_service.create_ticket(
        session,
        user=mock_user,
        kind="PO",
        document_type="YSER",
        form={"header": {}, "lines": [{}]},
        parent_pr_id=parent_id,
        files=[],
    )
    assert ticket.parent_pr_id == parent_id
    assert ticket.sap_sync.get("sync_pending") is True


async def test_po_parent_other_user_raises(mock_user: MagicMock, stub_form_pipeline: None) -> None:
    parent_id = uuid.uuid4()
    pr = MagicMock(
        kind="PR",
        sap_id="MOCK-PR-OK",
        created_by_user_id=uuid.uuid4(),
        document_type="YSER",
    )
    session = _session_for_create_ticket(get_return=pr)
    with pytest.raises(ValueError, match="Invalid parent PR"):
        await proc_service.create_ticket(
            session,
            user=mock_user,
            kind="PO",
            document_type="YSER",
            form={"header": {}, "lines": [{}]},
            parent_pr_id=parent_id,
            files=[],
        )


async def test_po_parent_document_type_mismatch_raises(mock_user: MagicMock, stub_form_pipeline: None) -> None:
    parent_id = uuid.uuid4()
    pr = MagicMock(
        kind="PR",
        sap_id="MOCK-PR-OK",
        created_by_user_id=mock_user.id,
        document_type="YUNB",
    )
    session = _session_for_create_ticket(get_return=pr)
    with pytest.raises(ValueError, match="workflow type must match"):
        await proc_service.create_ticket(
            session,
            user=mock_user,
            kind="PO",
            document_type="YSER",
            form={"header": {}, "lines": [{}]},
            parent_pr_id=parent_id,
            files=[],
        )


def test_po_create_payload_parent_pr_uuid_invalid_raises_value_error() -> None:
    body = ProcurementPoCreateFormPayload(document_type="YSER", form={}, parent_pr_id="not-a-uuid")
    with pytest.raises(ValueError, match="Invalid parent_pr_id"):
        body.parent_pr_uuid()


async def test_po_second_linked_from_same_pr_raises(
    mock_user: MagicMock, stub_form_pipeline: None
) -> None:
    parent_id = uuid.uuid4()
    pr = MagicMock(
        kind="PR",
        sap_id="MOCK-PR-OK",
        created_by_user_id=mock_user.id,
        document_type="YSER",
    )
    session = _session_for_create_ticket(get_return=pr)
    linked_po_id = uuid.uuid4()
    lock_res = MagicMock()
    child_res = MagicMock()
    child_res.scalar_one_or_none.return_value = linked_po_id
    session.execute = AsyncMock(side_effect=[lock_res, child_res])
    with pytest.raises(PrAlreadyHasLinkedPoError) as exc_info:
        await proc_service.create_ticket(
            session,
            user=mock_user,
            kind="PO",
            document_type="YSER",
            form={"header": {}, "lines": [{}]},
            parent_pr_id=parent_id,
            files=[],
        )
    assert exc_info.value.linked_po_id == linked_po_id


@pytest.mark.asyncio
async def test_ensure_pr_available_for_po_link_locks_parent_then_checks_child() -> None:
    parent_id = uuid.uuid4()
    session = MagicMock()
    lock_res = MagicMock()
    free_res = MagicMock()
    free_res.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[lock_res, free_res])
    await proc_service.ensure_pr_available_for_po_link(session, parent_pr_id=parent_id)
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_list_parent_prs_for_po_excludes_linked_pr() -> None:
    user_id = uuid.uuid4()
    free_pr = MagicMock()
    free_pr.id = uuid.uuid4()
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [free_pr]
    session.execute = AsyncMock(return_value=result)
    rows = await proc_service.list_parent_prs_for_po(session, user_id=user_id)
    assert rows == [free_pr]
    stmt = session.execute.await_args.args[0]
    sql = str(stmt)
    assert "parent_pr_id" in sql.lower()
