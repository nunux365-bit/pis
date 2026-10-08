"""Pipeline review-queue transitions — preserve audit trail and respect FSM.

Pure-function tests — they only exercise the in-memory state of an
``EmailAutomationSend`` row by stubbing the small DB surface ``mark_*`` uses.
This keeps the suite hermetic (no Postgres) while still pinning the contract
the four-persona reviews flagged: rejection must NOT overwrite the original
``review_reasons`` (auditors need it), approval must record the actor + note,
and retry only flips ``failed`` rows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from app.email_automation import pipeline


@dataclass
class _Row:
    id: UUID = field(default_factory=uuid4)
    status: str = "rendered"
    review_reasons: list[dict] | None = None
    approved_by: UUID | None = None
    approved_at: object | None = None
    send_attempt_count: int = 0


class _FakeResult:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> _Row | None:
        return self._row


class _FakeDB:
    """Just enough of ``AsyncSession`` to satisfy ``mark_*`` flows."""

    def __init__(self, row: _Row | None) -> None:
        self._row = row
        self.executed: list[Any] = []

    async def execute(self, stmt):  # noqa: ANN001
        self.executed.append(stmt)
        return _FakeResult(self._row)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.new_event_loop().run_until_complete(coro)


def test_mark_approved_records_actor_and_note_and_preserves_reasons():
    row = _Row(
        status="rendered",
        review_reasons=[{"code": "no_primary_recipient", "detail": "tracker empty"}],
    )
    db = _FakeDB(row)
    actor = uuid4()
    ok = asyncio.new_event_loop().run_until_complete(
        pipeline.mark_approved(db, row.id, actor, note="approved on call w/ Bhawna")
    )
    assert ok is True
    assert row.status == "approved"
    assert row.approved_by == actor
    # Original reason kept; approval event appended.
    codes = [r["code"] for r in row.review_reasons]
    assert codes == ["no_primary_recipient", "approved"]
    appended = row.review_reasons[-1]
    assert appended["by"] == str(actor)
    assert appended["note"] == "approved on call w/ Bhawna"


def test_mark_rejected_appends_and_does_not_overwrite_review_reasons():
    row = _Row(
        status="rendered",
        review_reasons=[
            {"code": "tracker_disabled", "detail": "Status=Inactive"},
            {"code": "no_primary_recipient", "detail": "TO empty"},
        ],
    )
    db = _FakeDB(row)
    ok = asyncio.new_event_loop().run_until_complete(
        pipeline.mark_rejected(db, row.id, "Customer no longer billed", actor_id=uuid4())
    )
    assert ok is True
    assert row.status == "skipped"
    codes = [r["code"] for r in row.review_reasons]
    # Both originals preserved + rejection appended (auditor invariant).
    assert codes == ["tracker_disabled", "no_primary_recipient", "rejected"]
    assert row.review_reasons[-1]["detail"] == "Customer no longer billed"


def test_mark_retry_failed_only_resets_failed_rows():
    failed_row = _Row(status="failed")
    db = _FakeDB(failed_row)
    ok = asyncio.new_event_loop().run_until_complete(
        pipeline.mark_retry_failed(db, failed_row.id, uuid4())
    )
    assert ok is True
    assert failed_row.status == "approved"
    assert failed_row.review_reasons[-1]["code"] == "retry_requested"


def test_mark_retry_failed_returns_false_when_row_not_failed():
    db = _FakeDB(None)  # query returns None — not 'failed'
    ok = asyncio.new_event_loop().run_until_complete(
        pipeline.mark_retry_failed(db, uuid4(), uuid4())
    )
    assert ok is False


def test_mark_approved_returns_false_when_row_not_in_reviewable_state():
    db = _FakeDB(None)  # query filters to rendered only
    ok = asyncio.new_event_loop().run_until_complete(
        pipeline.mark_approved(db, uuid4(), uuid4())
    )
    assert ok is False


def test_period_key_uses_received_at_when_present():
    import datetime as dt

    received = dt.datetime(2026, 4, 13, 9, 0, tzinfo=dt.timezone.utc)
    fallback = dt.datetime(2030, 12, 31, tzinfo=dt.timezone.utc)
    assert pipeline.period_key("iso_week", received, fallback=fallback) == "2026-W16"


def test_period_key_uses_fallback_only_when_received_at_missing():
    """Idempotency: identical fallback → identical key across retries."""

    import datetime as dt

    fallback = dt.datetime(2026, 4, 13, 9, 0, tzinfo=dt.timezone.utc)
    k1 = pipeline.period_key("iso_week", None, fallback=fallback)
    k2 = pipeline.period_key("iso_week", None, fallback=fallback)
    assert k1 == k2 == "2026-W16"
