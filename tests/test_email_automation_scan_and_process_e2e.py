"""End-to-end integration tests for ``scan_and_process`` and dispatch.

Exercises the full pipeline against a real Postgres (same fixture pattern the
rest of this suite uses — skipped gracefully when the stack isn't running).

What we mock vs. what we run for real:

* Gmail API (``list_inbox_messages`` / ``fetch_message`` / ``download_attachment``
  / ``send_email``) — mocked; the agent's job is to orchestrate, not to hit
  Google.
* Google Sheets (``read_table``) — mocked; returns a synthetic master-tracker
  table so resolver can produce TO/CC.
* The actual xlsx ingestion, variant filtering, aggregation, rendering, dedupe
  key generation, idempotent ``ON CONFLICT`` upsert, ``SELECT ... FOR UPDATE
  SKIP LOCKED`` claim in dispatch, ``send_attempt_count`` increment — all real,
  running against Postgres.

Each test is self-contained: inserts its own ``EmailAutomationMessage`` /
``EmailAutomationSend`` rows keyed by a unique ``provider_message_id`` and
deletes them in teardown so parallel runs + reruns are hermetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from openpyxl import Workbook
from sqlalchemy import delete, select

from app.config.settings import settings
from app.db.models import EmailAutomationMessage, EmailAutomationSend
from app.email_automation import gmail_sa, sheets_sa
from app.email_automation.pipeline import (
    dispatch_claim_and_send,
    scan_and_process,
)


def _limit_payment_reminder_pack_to_epharma(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E workbook only models ePharma tabs; CHW needs ``Invoice details-T(labs)`` etc."""

    from app.email_automation.workflow_packs import payment_reminder as pr_pm

    _orig = pr_pm.WorkflowPack.iter_variants

    def _iter_variants(self):  # noqa: ANN001
        if self.workflow_type == "PAYMENT_REMINDER_WEEKLY":
            return (pr_pm.EPHARMA_VARIANT,)
        return _orig(self)

    monkeypatch.setattr(pr_pm.WorkflowPack, "iter_variants", _iter_variants)


# ---------------------------------------------------------------------------
# Minimal synthetic workbook: one ePharma row that passes all 5 SOP filters.
# ---------------------------------------------------------------------------


def _write_invoice_h_psp(ws) -> None:
    """``Invoice details-H(all)&T(Psp)`` — header at row 2 (0-indexed)."""

    ws.append([])
    ws.append([])
    ws.append(
        [
            "Co", "Code", "HANA Code", "Name of the party", "Brand Name",
            "Business Owner", "KAM 2", "RPT", "KAM",
            "Invoice Date", "Invoice No:-", "Credit Days",
            "Original Amount from Aug'24", "Net Amount Pending",
            "Write off Amount", "Ap Adjustment/CN", "CW Recipts",
            "PLA", "Old Receipts", "Receipts-Mar", "26AS",
            "TDS/Adjustment", "SD Amount", "Final Amount Pending",
            "PGB/CPGB Amount/Rent", "Location", "Days", "Due Days",
            "Ageing type", "Ageing type-Audit", "Remarks", "Consider",
            "Mapping type", "Entry code", "Mapping status",
            "Business transfer Invoice", "Business Unit", "Retail",
            "Segment", "Action", "PLA Remarks", "Program Details-PSP",
            "Buisness Unit Old( 27th may'24)", "Check", "PO Number", "Period",
        ]
    )
    ws.append(
        [
            "TATA 1MGH", "OTI-079", "E2E-HANA-0001",
            "E2E SUN PHARMA E2E TEST CORP",
            "Cetaphil", "Prateek", "Sajal", None, "Vikas",
            datetime(2026, 1, 15), "HH-E2E-0001", 75,
            500000, 250000,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 250000,
            0, "Mumbai", 90, 60,
            "1-3 Months", None, "Invoice", "Positive",
            None, None, None, None, "e-Pharmacy", None,
            "Advertisement", None, None, None, None, None, None, None,
        ]
    )


def _write_party_wise(ws) -> None:
    """``Party wise Ageing-H&T`` — header at row 4. Used for unaccounted lookup."""

    for _ in range(4):
        ws.append([])
    ws.append(
        [
            "Code", "Name of the party", "KAM 2", "RPT", "Business Unit",
            "Segment", "Business Owner", "Credit Days",
            "Not Due", "0-1 Months", "1-3 Months", "3-6 Months",
            "6-9 Months", "9-12 Months", "+1yrs", "Grand Total",
            "Unaccounted receipts", "Amount in SAP", "Remarks",
        ]
    )
    ws.append(
        [
            "E2E-HANA-0001", "E2E SUN PHARMA E2E TEST CORP", "Sajal", "", "e-Pharmacy",
            "Advertisement", "Prateek", 15,
            0, 0, 250000, 0, 0, 0, 0, 250000,
            0, 0, "",  # zero unaccounted ⇒ send proceeds (total_outstanding > 0)
        ]
    )


def _build_workbook(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice details-H&T"
    _write_invoice_h_psp(ws)
    ws2 = wb.create_sheet("Party wise Ageing-H&T")
    _write_party_wise(ws2)
    wb.save(str(path))


# ---------------------------------------------------------------------------
# Gmail + Sheets fakes — thin enough to keep the test readable.
# ---------------------------------------------------------------------------


@dataclass
class _FakeAttachment:
    filename: str
    mime_type: str
    size_bytes: int
    gmail_attachment_id: str
    part_id: str | None = None

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "gmail_attachment_id": self.gmail_attachment_id,
            "part_id": self.part_id,
        }


def _install_gmail_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message_id: str,
    xlsx_src: Path,
    sent_sink: list[dict[str, Any]] | None = None,
) -> None:
    """Pin deterministic Gmail responses and a send-capture hook."""

    attachment = gmail_sa.AttachmentStub(
        filename="Receivable-E2E.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=xlsx_src.stat().st_size,
        gmail_attachment_id=f"ATT-{message_id}",
        part_id="1.1",
    )
    fetched = gmail_sa.FetchedMessage(
        id=message_id,
        thread_id=f"THREAD-{message_id}",
        sender="Bhawna Gandhi <bhawna.gandhi@1mg.com>",
        subject="Receivable / Overdue as on Date 13 Apr 2026",
        received_at_ms=int(datetime(2026, 4, 13, 9, 0).timestamp() * 1000),
        headers={
            "From": "Bhawna Gandhi <bhawna.gandhi@1mg.com>",
            "Subject": "Receivable / Overdue as on Date 13 Apr 2026",
            "Message-ID": f"<{message_id}@mail.1mg.com>",
        },
        attachments=(attachment,),
    )

    monkeypatch.setattr(
        gmail_sa, "list_inbox_messages",
        lambda query, max_results=50: [message_id],
    )
    monkeypatch.setattr(gmail_sa, "fetch_message", lambda mid: fetched)

    def _download(mid, att_id, filename, target_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / filename
        out.write_bytes(xlsx_src.read_bytes())
        return out

    monkeypatch.setattr(gmail_sa, "download_attachment", _download)

    if sent_sink is not None:
        def _send(**kwargs):
            sent_sink.append(kwargs)
            return f"SENT-{len(sent_sink):04d}"

        monkeypatch.setattr(gmail_sa, "send_email", _send)

    monkeypatch.setattr(
        gmail_sa,
        "fetch_message_rfc_message_id",
        lambda mid: f"<rfc-{mid}@agentos.test>",
    )


def _install_sheets_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a single-row ePharma master tracker for ``E2E-HANA-0001``."""

    headers = (
        "KAM", "BP Code", "Billed to Entity Name",
        "KAM Email", "Email 1", "Email 2",
    )
    rows = (
        (
            "Vikas", "E2E-HANA-0001", "E2E SUN PHARMA E2E TEST CORP",
            "vikas.singh2+e2e@1mg.com", "ar.team+e2e@sunpharma.example",
            "",
        ),
    )
    table = sheets_sa.SheetTable(headers=headers, rows=rows)
    monkeypatch.setattr(
        sheets_sa, "read_table",
        lambda sid, tab, header_row_index=0, use_cache=True: table,
    )


# ---------------------------------------------------------------------------
# Skippable PG fixture — mirrors ``client_employee`` in conftest.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_or_skip():
    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as s:
            await s.execute(select(EmailAutomationMessage).limit(1))
    except Exception:
        pytest.skip("Postgres / email_automation schema not available")


# ---------------------------------------------------------------------------
# Cleanup helper — we key every inserted row on a unique ``provider_message_id``
# so teardown is a two-statement DELETE cascade-style scrub.
# ---------------------------------------------------------------------------


async def _scrub(provider_message_id: str) -> None:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        msg = (
            await s.execute(
                select(EmailAutomationMessage).where(
                    EmailAutomationMessage.provider_message_id == provider_message_id
                )
            )
        ).scalar_one_or_none()
        if msg is not None:
            await s.execute(
                delete(EmailAutomationSend).where(
                    EmailAutomationSend.source_message_id == msg.id
                )
            )
            await s.execute(
                delete(EmailAutomationMessage).where(
                    EmailAutomationMessage.id == msg.id
                )
            )
            await s.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_and_process_happy_path_creates_approved_send(
    tmp_path: Path,
    pg_or_skip,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingest → classify → one ePharma client plan → one ``approved`` send row.

    Guard-rails asserted by this test (everything listed here is a real
    regression we hit earlier in the build):

      * ``provider_message_id`` ingest is idempotent (single row even though
        the message already exists from a previous crashed tick).
      * The dedupe key lands the send in ``approved`` (auto-send; no HITL).
      * ``test_mode=True`` is snapshotted on the send row.
      * ``resolved_to``/``resolved_cc`` come from the *mocked* sheets tracker,
        not from any real Google call.
      * Attachment staging dir is cleaned up (xlsx file deleted after run).
    """

    message_id = "E2E-MSG-SCAN-001"
    await _scrub(message_id)
    xlsx_src = tmp_path / "Receivable-E2E.xlsx"
    _build_workbook(xlsx_src)

    _install_gmail_fakes(monkeypatch, message_id=message_id, xlsx_src=xlsx_src)
    _install_sheets_fake(monkeypatch)

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    # Pin query so local ``.env`` overrides cannot drop ``from:bhawna...`` and
    # empty the classifier allowlist (would yield ``classified=None``, 0 sends).
    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        'from:bhawna.gandhi@1mg.com '
        'subject:"Receivable / Overdue as on Date" '
        "newer_than:14d",
    )
    monkeypatch.setattr(settings, "email_automation_test_mode", True)
    monkeypatch.setattr(settings, "email_automation_require_approval", False)
    monkeypatch.setattr(settings, "email_automation_epharma_master_sheet_id", "SHEET-E2E")
    monkeypatch.setattr(settings, "email_automation_epharma_master_tab", "Sheet3")
    monkeypatch.setattr(
        settings, "email_automation_attachment_dir", str(tmp_path / "staging"),
    )

    _limit_payment_reminder_pack_to_epharma(monkeypatch)

    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as s:
            outcome = await scan_and_process(s)
        assert outcome["message_count"] == 1
        assert outcome["send_count"] >= 1, outcome
        assert message_id in outcome["scanned_ids"]

        async with AsyncSessionLocal() as s:
            msg = (
                await s.execute(
                    select(EmailAutomationMessage).where(
                        EmailAutomationMessage.provider_message_id == message_id
                    )
                )
            ).scalar_one()
            sends = (
                await s.execute(
                    select(EmailAutomationSend).where(
                        EmailAutomationSend.source_message_id == msg.id
                    )
                )
            ).scalars().all()

        assert msg.status == "processed"
        assert msg.workflow_type == "PAYMENT_REMINDER_WEEKLY"

        epharma = [s for s in sends if s.variant == "epharma"]
        assert len(epharma) == 1, [s.variant for s in sends]
        row = epharma[0]
        assert row.business_key == "E2E-HANA-0001"
        assert row.status == "approved"
        assert row.test_mode is True
        assert "ar.team+e2e@sunpharma.example" in row.resolved_to_addrs
        assert "vikas.singh2+e2e@1mg.com" in row.resolved_cc_addrs
        assert row.rendered_subject
        assert row.rendered_body_html

        staging = tmp_path / "staging" / message_id
        assert not (staging / "Receivable-E2E.xlsx").exists()
    finally:
        await _scrub(message_id)


@pytest.mark.asyncio
async def test_dispatch_sends_approved_row_and_increments_attempt_count(
    tmp_path: Path,
    pg_or_skip,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After scan lands an ``approved`` row, dispatch sends it via (mocked) Gmail,
    redirects to the automation mailbox in test mode (printing real recipients),
    and flips status to ``sent`` with ``send_attempt_count == 1``.

    Uses one asyncio loop for all DB work so asyncpg matches pytest-asyncio
    (multiple ``new_event_loop()`` instances break the pool).
    """

    message_id = "E2E-MSG-DISPATCH-001"
    await _scrub(message_id)
    xlsx_src = tmp_path / "Receivable-E2E.xlsx"
    _build_workbook(xlsx_src)

    sent_sink: list[dict[str, Any]] = []
    _install_gmail_fakes(
        monkeypatch, message_id=message_id, xlsx_src=xlsx_src, sent_sink=sent_sink,
    )
    _install_sheets_fake(monkeypatch)

    monkeypatch.setattr(settings, "email_automation_enabled", True)
    monkeypatch.setattr(
        settings,
        "email_automation_inbox_query",
        'from:bhawna.gandhi@1mg.com '
        'subject:"Receivable / Overdue as on Date" '
        "newer_than:14d",
    )
    monkeypatch.setattr(settings, "email_automation_test_mode", True)
    monkeypatch.setattr(settings, "email_automation_test_redirect_to", "automation.agents@1mg.com")
    monkeypatch.setattr(settings, "email_automation_require_approval", False)
    monkeypatch.setattr(settings, "email_automation_epharma_master_sheet_id", "SHEET-E2E")
    monkeypatch.setattr(settings, "email_automation_epharma_master_tab", "Sheet3")
    monkeypatch.setattr(
        settings, "email_automation_attachment_dir", str(tmp_path / "staging"),
    )

    _limit_payment_reminder_pack_to_epharma(monkeypatch)

    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as s:
            await scan_and_process(s)
        async with AsyncSessionLocal() as s:
            outcome = await dispatch_claim_and_send(s, limit=10)

        assert len(outcome["sent"]) >= 1, outcome
        assert outcome["failed"] == []

        assert len(sent_sink) >= 1
        call = sent_sink[0]
        assert call["to"] == ["automation.agents@1mg.com"]
        # First-ever send for this business_key: no cached prior RFC id —
        # threads off inbound trigger Message-ID from ingest.
        hdrs = call.get("headers") or {}
        assert hdrs.get("In-Reply-To") == f"<{message_id}@mail.1mg.com>"
        assert hdrs.get("References") == f"<{message_id}@mail.1mg.com>"

        async with AsyncSessionLocal() as s:
            msg = (
                await s.execute(
                    select(EmailAutomationMessage).where(
                        EmailAutomationMessage.provider_message_id == message_id
                    )
                )
            ).scalar_one()
            sends = (
                await s.execute(
                    select(EmailAutomationSend).where(
                        EmailAutomationSend.source_message_id == msg.id
                    )
                )
            ).scalars().all()

        sent_rows = [r for r in sends if r.status == "sent"]
        assert sent_rows, [s.status for s in sends]
        assert sent_rows[0].send_attempt_count == 1
        assert sent_rows[0].provider_message_id
        assert sent_rows[0].provider_rfc_message_id == (
            f"<rfc-{sent_rows[0].provider_message_id}@agentos.test>"
        )
    finally:
        await _scrub(message_id)
