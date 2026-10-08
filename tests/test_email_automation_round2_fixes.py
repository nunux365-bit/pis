"""Round-2 hardening: header injection, dispatch reclaim correctness."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.email_automation import pipeline
from app.email_automation.engine.renderer import (
    RendererConfig,
    render,
)
from app.email_automation.engine.types import AggregatedClient, NormalizedRow


# ---------------------------------------------------------------------------
# Subject header injection
# ---------------------------------------------------------------------------


def test_subject_strips_crlf_to_prevent_header_injection():
    """A tracker-supplied party_name with embedded ``\\r\\n`` must NOT survive
    into the subject — otherwise it forges SMTP headers (Bcc, X-Spam, …)."""

    rows = [
        NormalizedRow(
            source_sheet="H(all)",
            values={"x#0": "v"},
            raw={},
        )
    ]
    client = AggregatedClient(
        business_key="H001",
        business_key_parts=("H001",),
        party_name="Acme\r\nBcc: attacker@evil.com",
        rows=rows,
        totals={"0-1 months#0": Decimal("100")},
    )
    cfg = RendererConfig(
        subject_template="Reminder {party_name} {total_outstanding_inr}",
        body_template="<p>{party_name}</p>",
        invoice_columns=(),
    )
    subject, _ = render(client, cfg)
    # The CR/LF must be replaced by spaces; no newline character may survive.
    assert "\r" not in subject
    assert "\n" not in subject
    # Original characters become spaces — the operator still sees the (now-flat) string.
    assert "Acme" in subject
    assert "attacker@evil.com" in subject


def test_subject_strips_extra_context_crlf():
    rows = [NormalizedRow(source_sheet="x", values={"x#0": "v"}, raw={})]
    client = AggregatedClient(
        business_key="K", business_key_parts=("K",), party_name="P",
        rows=rows, totals={},
    )
    cfg = RendererConfig(
        subject_template="Sub {custom_field}",
        body_template="<p>{custom_field}</p>",
        invoice_columns=(),
    )
    subject, body = render(
        client, cfg, extra_context={"custom_field": "ok\r\nBcc: x@y.com"}
    )
    assert "\r" not in subject and "\n" not in subject
    # Body context is HTML-escaped (preserved structure but inert).
    assert "ok" in body


# ---------------------------------------------------------------------------
# Dispatch reclaim — exactly-once heuristic
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    id: UUID = field(default_factory=uuid4)
    status: str = "sending"
    provider_message_id: str | None = None
    sent_at: datetime | None = None
    review_reasons: list[dict] | None = None
    error: Any = None
    # Fields surfaced in structured reclaim logs (F1); filled with neutral
    # defaults so the fake stays minimal but the logger call doesn't crash.
    workflow_type: str = "W"
    variant: str = "v"
    business_key: str = "K"
    period_key: str = "2026-W01"
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) - timedelta(hours=1)
    )


class _RowsResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self):
        class _S:
            def __init__(s, r): s._r = r
            def all(s): return s._r
        return _S(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.commit_count = 0

    async def execute(self, _stmt):
        return _RowsResult(self._rows)

    async def commit(self):
        self.commit_count += 1


def test_reclaim_marks_completed_when_provider_id_already_set():
    """Crashed-after-Gmail-accept rows must NOT be re-sent."""

    row = _Row(provider_message_id="gmail-id-99")
    db = _FakeDB([row])
    out = asyncio.new_event_loop().run_until_complete(pipeline.reclaim_stuck_sending(db))
    assert out == {"requeued": 0, "completed": 1}
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.review_reasons[-1]["code"] == "reclaim_already_sent"


def test_reclaim_flags_indeterminate_when_no_provider_id():
    """Crashed-with-no-provider-id rows are indeterminate, not safe to re-send.

    No-HITL system: the row is marked ``failed`` with a
    ``reclaim_indeterminate`` reason and a clear suggested action. Silent
    re-queue would risk a double send if Gmail accepted the message after
    the SA call returned but before our DB commit. Ops can manually re-enable
    via the retry endpoint after verifying Gmail Sent.
    """

    row = _Row(provider_message_id=None)
    db = _FakeDB([row])
    out = asyncio.new_event_loop().run_until_complete(pipeline.reclaim_stuck_sending(db))
    assert out == {"requeued": 1, "completed": 0}  # backwards-compatible counter
    assert row.status == "failed"
    last = row.review_reasons[-1]
    assert last["code"] == "reclaim_indeterminate"
    assert "human_message" in last
    assert "suggested_action" in last
    # The error column carries a machine-readable hint for the ops dashboard.
    assert row.error is not None
    assert row.error["error"] == "reclaim_indeterminate"


def test_reclaim_handles_mixed_batch():
    completed = _Row(provider_message_id="ok")
    indeterminate = _Row(provider_message_id=None)
    db = _FakeDB([completed, indeterminate])
    out = asyncio.new_event_loop().run_until_complete(pipeline.reclaim_stuck_sending(db))
    assert out == {"requeued": 1, "completed": 1}
    assert completed.status == "sent"
    assert indeterminate.status == "failed"
