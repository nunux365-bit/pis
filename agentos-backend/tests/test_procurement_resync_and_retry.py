"""Unit checks for procurement PATCH defaults and SAP retry query shape."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.schemas.procurement import ProcurementTicketUpdateBody as TicketUpdateBody
from app.procurement import service as proc_service


def test_sap_failure_retryable_skips_auth_errors() -> None:
    assert proc_service._sap_failure_retryable("Unauthorized — check SAP credentials") is False
    assert proc_service._sap_failure_retryable("SAP connection error: timeout") is True


def test_sap_failure_retryable_skips_po_validation_errors() -> None:
    assert proc_service._sap_failure_retryable("No PO line items to send to SAP") is False
    assert proc_service._sap_failure_retryable("Vendor (Supplier) is required for SAP PO create") is False


def test_ticket_update_resync_sap_defaults_false():
    b = TicketUpdateBody(version=3)
    assert b.resync_sap is False


def test_should_recover_before_create_skips_first_ui_submit() -> None:
    assert proc_service._should_recover_before_create({"create_submitted": False, "attempt_count": 0}) is False


def test_should_recover_before_create_after_create_post() -> None:
    assert proc_service._should_recover_before_create({"create_submitted": True, "attempt_count": 0}) is True


def test_should_recover_before_create_on_resync_after_failure() -> None:
    assert proc_service._should_recover_before_create({"create_submitted": False, "attempt_count": 2}) is True


def test_sap_retry_select_uses_json_filters_and_limit():
    now = datetime.now(UTC)
    q = proc_service.sap_retry_ticket_select(6, 50, now)
    sql = str(q.compile(dialect=postgresql.dialect()))
    assert "sap_sync ->>" in sql
    assert "retryable" in sql or "sap_sync" in sql
    assert "sap_id" in sql
    assert "LIMIT" in sql.upper()
    assert "FOR UPDATE" in sql.upper()
    assert "SKIP LOCKED" in sql.upper()


def test_sap_retry_job_commits_per_ticket():
    import inspect

    src = inspect.getsource(proc_service.sap_retry_job_batch)
    assert "sap_retry_ticket_select(max_attempts, 1, now)" in src
    assert "await session.commit()" in src


def test_attachment_retry_select_is_one_row_skip_locked():
    sql = str(proc_service.attachment_retry_ticket_select().compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql.upper()
    assert "SKIP LOCKED" in sql.upper()
    assert "LIMIT" in sql.upper()


def test_mark_sap_sync_deferred_resets_create_submitted_without_sap_id() -> None:
    import uuid

    from app.db.models import ProcurementTicket
    from app.procurement.service import _mark_sap_sync_deferred

    ticket = ProcurementTicket(
        id=uuid.uuid4(),
        kind="PR",
        document_type="YUNB",
        form={"header": {}, "lines": []},
        attachments=[],
        sap_id=None,
        sap_sync={"create_submitted": True, "attempt_count": 2, "sync_pending": False},
        version=1,
        created_by_user_id=uuid.uuid4(),
    )
    _mark_sap_sync_deferred(ticket)
    assert ticket.sap_sync["create_submitted"] is False
    assert ticket.sap_sync["sync_pending"] is True
    assert ticket.sap_sync["last_error"] is None


@pytest.mark.asyncio
async def test_recover_sap_id_before_create_skips_when_sap_id_set() -> None:
    import uuid
    from unittest.mock import AsyncMock

    from app.db.models import ProcurementTicket
    from app.procurement.service import _recover_sap_id_before_create

    ticket = ProcurementTicket(
        id=uuid.uuid4(),
        kind="PR",
        document_type="YUNB",
        form={"header": {}, "lines": []},
        attachments=[],
        sap_id="1040001234",
        sap_sync={},
        version=1,
        created_by_user_id=uuid.uuid4(),
    )
    recover = AsyncMock(return_value=("999", None))
    session = AsyncMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.procurement.service.sap_sync.try_recover_pr", recover)
        result = await _recover_sap_id_before_create(
            ticket,
            session,
            kind_u="PR",
            doc_type="YUNB",
            existing_sid="1040001234",
        )
    assert result == "1040001234"
    recover.assert_not_called()
    session.refresh.assert_not_called()


@pytest.mark.asyncio
async def test_recover_sap_id_before_create_runs_pr_recovery_when_no_sap_id() -> None:
    import uuid
    from unittest.mock import AsyncMock

    from app.db.models import ProcurementTicket
    from app.procurement.service import _recover_sap_id_before_create

    ticket = ProcurementTicket(
        id=uuid.uuid4(),
        kind="PR",
        document_type="YUNB",
        form={"header": {}, "lines": []},
        attachments=[],
        sap_id=None,
        sap_sync={},
        version=1,
        created_by_user_id=uuid.uuid4(),
    )
    recover = AsyncMock(return_value=("1040005678", None))
    session = AsyncMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.procurement.service.sap_sync.try_recover_pr", recover)
        result = await _recover_sap_id_before_create(
            ticket,
            session,
            kind_u="PR",
            doc_type="YUNB",
            existing_sid="",
        )
    assert result == "1040005678"
    recover.assert_awaited_once_with(ticket_id=str(ticket.id), document_type="YUNB")


@pytest.mark.asyncio
async def test_recover_sap_id_before_create_allows_create_on_miss() -> None:
    import uuid
    from unittest.mock import AsyncMock

    from app.db.models import ProcurementTicket
    from app.procurement.service import _recover_sap_id_before_create

    ticket = ProcurementTicket(
        id=uuid.uuid4(),
        kind="PO",
        document_type="YUNB",
        form={"header": {}, "lines": []},
        attachments=[],
        sap_id=None,
        sap_sync={},
        version=1,
        created_by_user_id=uuid.uuid4(),
    )
    recover = AsyncMock(return_value=(None, None))
    session = AsyncMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.procurement.service.sap_sync.try_recover_po", recover)
        result = await _recover_sap_id_before_create(
            ticket,
            session,
            kind_u="PO",
            doc_type="YUNB",
            existing_sid="",
        )
    assert result is None
    recover.assert_awaited_once()


@pytest.mark.asyncio
async def test_hydrate_clears_stale_lines_when_sap_has_no_active_items() -> None:
    import uuid
    from unittest.mock import AsyncMock

    from app.db.models import ProcurementTicket
    from app.procurement.sap_ticket_form_read import SAP_NO_ACTIVE_LINES_PR
    from app.procurement.service import _hydrate_ticket_form_from_sap

    ticket = ProcurementTicket(
        id=uuid.uuid4(),
        kind="PR",
        document_type="YSER",
        form={"header": {"purchasing_org": "1000"}, "lines": [{"short_text": "stale line"}]},
        attachments=[],
        sap_id="1010000926",
        sap_sync={},
        version=3,
        created_by_user_id=uuid.uuid4(),
    )
    load = AsyncMock(return_value=(None, "db", SAP_NO_ACTIVE_LINES_PR))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.procurement.service.load_form_from_sap", load)
        mp.setattr("app.procurement.service.settings.procurement_sap_hydrate_on_read", True)
        out = await _hydrate_ticket_form_from_sap(ticket)
    assert getattr(out, "sap_no_active_lines") is True
    assert out.form["lines"] == []
    assert out.form["header"]["purchasing_org"] == "1000"
    assert out.sap_form_read_error == SAP_NO_ACTIVE_LINES_PR
    load.assert_awaited_once()


@pytest.mark.asyncio
async def test_sap_retry_job_batch_commits_after_each_ticket(monkeypatch):
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db.models import ProcurementTicket

    uid = uuid.uuid4()
    user = SimpleNamespace(id=uid, email="a@b.c")
    tickets = [
        ProcurementTicket(
            id=uuid.uuid4(),
            kind="PR",
            document_type="YUNB",
            form={"header": {}, "lines": []},
            attachments=[],
            sap_id=None,
            sap_sync={"sync_pending": False, "retryable": True},
            version=1,
            created_by_user_id=uid,
        )
        for _ in range(2)
    ]
    queue = list(tickets)

    class _Result:
        def __init__(self, ticket):
            self._ticket = ticket

        def scalars(self):
            return self

        def first(self):
            return self._ticket

    class _Sess:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        async def execute(self, *_a, **_k):
            return _Result(queue.pop(0) if queue else None)

        async def get(self, *_a, **_k):
            return user

        def add(self, *_a):
            return None

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1

    sess = _Sess()
    monkeypatch.setattr(proc_service.settings, "procurement_sap_retry_batch_size", 10)
    monkeypatch.setattr(proc_service, "_run_sap_sync", AsyncMock())
    monkeypatch.setattr(proc_service, "_sync_attachments_after_sap", AsyncMock())
    n = await proc_service.sap_retry_job_batch(sess)
    assert n == 2
    assert sess.commits == 2
    assert sess.rollbacks == 0
    assert proc_service._run_sap_sync.await_count == 2

