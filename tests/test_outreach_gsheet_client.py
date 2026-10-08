"""Unit tests for outreach GSheet client — all Google API calls mocked."""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest
from app.email_automation.outreach.gsheet_client import (
    OutreachSheetClient,
    parse_address_list,
)


def test_parse_address_list_semicolon():
    assert parse_address_list("a@x.com; b@x.com") == ["a@x.com", "b@x.com"]


def test_parse_address_list_ampersand():
    assert parse_address_list("a@x.com & b@x.com") == ["a@x.com", "b@x.com"]


def test_parse_address_list_empty():
    assert parse_address_list("") == []
    assert parse_address_list(None) == []


def test_parse_address_list_single():
    assert parse_address_list("a@x.com") == ["a@x.com"]


@patch("app.email_automation.outreach.gsheet_client._build_sheets_service")
def test_read_prospect_tab_skips_meta_row(mock_build):
    """Row 1 is meta-annotations; Row 2 is headers; data starts Row 3."""
    svc = MagicMock()
    mock_build.return_value = svc
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "values": [
            # Row 1 — meta annotations (ignored)
            ["", "", "", "", "", "To", "", "", "", "", "CC", "BCC", "Agent reply", "Agent reply"],
            # Row 2 — actual headers
            ["Sr. No.", "Name of BD lead", "Account name", "SPOC", "Designation",
             "Email ID", "Industry", "Company size - employee count",
             "Target service 1", "Target service 2",
             "BD lead email ID", "BD head email ID", "Status", "Timestamp"],
            # Row 3 — first data row
            ["1", "Abhishek", "Infoedge", "Shamreen", "CHRO",
             "sharmeen@infoedge.com", "Recruitment", "6000",
             "AHC", "Subscription Plan",
             "abhishek.tiwari2@1mg.com", "vishakha.vartak@1mg.com", "", ""],
        ]
    }
    client = OutreachSheetClient(
        spreadsheet_id="fake_id",
        tab_name="Sheet1",
    )
    rows = client.read_prospect_rows()
    assert len(rows) == 1
    assert rows[0]["SPOC"] == "Shamreen"
    assert rows[0]["Email ID"] == "sharmeen@infoedge.com"
    assert rows[0]["_row_index"] == 3  # 1-based row number in the sheet


@patch("app.email_automation.outreach.gsheet_client._build_sheets_service")
def test_write_back_status(mock_build):
    # write_back_status now targets columns N:O (previously M:N)
    svc = MagicMock()
    mock_build.return_value = svc
    svc.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.return_value = {}
    client = OutreachSheetClient(
        spreadsheet_id="fake_id",
        tab_name="Sheet1",
    )
    client.write_back_status([(3, "sent", "2026-05-12T09:00:00+05:30")])
    svc.spreadsheets.return_value.values.return_value.batchUpdate.assert_called_once()


@patch("app.email_automation.outreach.gsheet_client._build_sheets_service")
def test_list_tab_names(mock_build):
    svc = MagicMock()
    mock_build.return_value = svc
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"title": "chw_cold_outreach_2026_04"}},
            {"properties": {"title": "chw_cold_outreach_2026_05"}},
        ]
    }
    client = OutreachSheetClient(
        spreadsheet_id="fake_id",
        tab_name="chw_cold_outreach_2026_05",
    )
    names = client.list_tab_names()
    assert names == ["chw_cold_outreach_2026_04", "chw_cold_outreach_2026_05"]


def test_write_back_reply_category_calls_batch_update():
    """write_back_reply_category calls spreadsheet batchUpdate with col P values."""
    client = OutreachSheetClient(
        spreadsheet_id="sheet_id",
        tab_name="Campaign_2026_05",
    )
    updates = [(2, "interested"), (5, "not_interested")]

    mock_svc = MagicMock()
    mock_gmail_sa = MagicMock()

    with (
        patch("app.email_automation.outreach.gsheet_client._build_sheets_service", return_value=mock_svc),
        patch("app.email_automation.outreach.gsheet_client.gmail_sa", mock_gmail_sa),
    ):
        client.write_back_reply_category(updates)

    mock_gmail_sa.call_with_retry.assert_called_once()
    call_args = mock_gmail_sa.call_with_retry.call_args[0]
    assert call_args[0] == "spreadsheets.values.batchUpdate"


def test_write_back_reply_category_noop_on_empty():
    """write_back_reply_category is a no-op when updates is empty."""
    client = OutreachSheetClient(
        spreadsheet_id="s", tab_name="t",
    )
    with patch("app.email_automation.outreach.gsheet_client._build_sheets_service") as mock_build:
        client.write_back_reply_category([])
    mock_build.assert_not_called()
