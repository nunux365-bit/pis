"""Tests for the daily GSheet → Postgres sync reconciliation logic."""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.email_automation.outreach.sync import reconcile_rows, _extract_primary_email


def test_extract_primary_email_single():
    assert _extract_primary_email("user@example.com") == "user@example.com"


def test_extract_primary_email_ampersand():
    assert _extract_primary_email("a@x.com & b@x.com") == "a@x.com"


def test_extract_primary_email_semicolon():
    assert _extract_primary_email("a@x.com; b@x.com") == "a@x.com"


def test_extract_primary_email_empty():
    assert _extract_primary_email("") == ""
    assert _extract_primary_email(None) == ""


def _sheet_row(email="test@test.com", spoc="Alice", col_m="", row_index=3):
    return {
        "Sr. No.": "1",
        "Name of BD lead": "Abhishek",
        "Account name": "TestCo",
        "SPOC": spoc,
        "Designation": "CHRO",
        "Email ID": email,
        "Industry": "IT",
        "Company size - employee count": "1000",
        "Target service 1": "AHC",
        "Target service 2": "",
        "BD lead email ID": "bd@1mg.com",
        "BD head email ID": "head@1mg.com",
        "Status": col_m,
        "Timestamp": "",
        "Source": "LinkedIn",
        "_row_index": row_index,
    }


def test_new_row_no_issues_becomes_pending():
    row = _sheet_row()  # valid email, spoc, etc.
    result = reconcile_rows(
        sheet_rows=[row],
        existing_leads={},
        campaign_name="chw_cold_outreach",
        tab_name="chw_cold_outreach_2026_05",
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert result["to_insert"][0]["status"] == "pending"


def test_source_field_stored():
    row = _sheet_row()  # includes "Source": "LinkedIn"
    result = reconcile_rows(
        sheet_rows=[row],
        existing_leads={},
        campaign_name="chw_cold_outreach",
        tab_name="chw_cold_outreach_2026_05",
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert result["to_insert"][0]["source"] == "LinkedIn"
    assert result["to_insert"][0]["tab_name"] == "chw_cold_outreach_2026_05"


def test_sent_lead_col_m_cleared_stays_sent():
    """Clearing Col M must not re-arm a sent lead."""
    existing = {"test@test.com": MagicMock(status="sent", sent_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                                           spoc="Alice", bd_lead_name="X", account_name="Y",
                                           designation="Z", email_id="test@test.com",
                                           industry="IT", company_size=1000,
                                           target_service_1="AHC", target_service_2="",
                                           bd_lead_email="", bd_head_email="", sr_no=1,
                                           send_attempt_count=1,
                                           issues_acknowledged_at=None,
                                           issues=[])}
    row = _sheet_row(col_m="")
    result = reconcile_rows(
        sheet_rows=[row],
        existing_leads=existing,
        campaign_name="chw_cold_outreach",
        tab_name="chw_cold_outreach_2026_05",
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    updates = {u["primary_email"]: u for u in result["to_update"]}
    assert updates.get("test@test.com", {}).get("status") != "pending"


def test_duplicate_emails_both_marked_duplicate():
    rows = [_sheet_row(row_index=3), _sheet_row(row_index=4)]  # same email twice
    result = reconcile_rows(
        sheet_rows=rows,
        existing_leads={},
        campaign_name="chw_cold_outreach",
        tab_name="chw_cold_outreach_2026_05",
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert all(r["status"] == "duplicate" for r in result["to_insert"])


def test_deleted_row_marked_removed_from_sheet():
    existing = {"test@test.com": MagicMock(status="pending", sent_at=None,
                                           spoc="Alice", bd_lead_name="X",
                                           account_name="Y", designation="Z",
                                           email_id="test@test.com", industry="IT",
                                           company_size=None, target_service_1="",
                                           target_service_2="", bd_lead_email="",
                                           bd_head_email="", sr_no=1,
                                           send_attempt_count=0,
                                           issues_acknowledged_at=None,
                                           issues=[])}
    result = reconcile_rows(
        sheet_rows=[],  # row deleted from sheet
        existing_leads=existing,
        campaign_name="chw_cold_outreach",
        tab_name="chw_cold_outreach_2026_05",
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    assert result["to_update"][0]["status"] == "removed_from_sheet"


def test_sent_lead_absent_from_new_tab_not_marked_removed():
    """Sent leads from previous months must survive tab rotation."""
    existing = {"test@test.com": MagicMock(
        status="sent", sent_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        spoc="Alice", bd_lead_name="X", account_name="Y", designation="Z",
        email_id="test@test.com", industry="IT", company_size=1000,
        target_service_1="AHC", target_service_2="", bd_lead_email="",
        bd_head_email="", sr_no=1, send_attempt_count=1,
        issues_acknowledged_at=None, issues=[])}
    result = reconcile_rows(
        sheet_rows=[],  # new month tab is empty
        existing_leads=existing,
        campaign_name="chw_cold_outreach",
        tab_name="chw_cold_outreach_2026_05",
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )
    # must NOT be marked removed_from_sheet
    assert not any(u.get("status") == "removed_from_sheet" for u in result["to_update"])
