"""Unit tests for extracted sync helpers."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.email_automation.outreach.config import CampaignConfig
from app.email_automation.outreach.sync import ingest_campaign_sheet, reconcile_campaign_db


def _fake_cfg() -> CampaignConfig:
    return CampaignConfig(
        campaign_name="test_campaign",
        gsheet_id="sheet123",
        tab_name="test_campaign_2026_05",
        sender_email="sender@example.com",
        subject="Test outreach subject",
        body_template_path="chw_outreach.html",
        category_prompt_path="prompts/chw_outreach_reply.txt",
        attachment_path=None,
        dashboard_display_name="Test Campaign",
        visible_columns=["SPOC", "Account name", "Status"],
    )


@pytest.mark.asyncio
async def test_ingest_campaign_sheet_returns_rows():
    """ingest_campaign_sheet returns tab_name + rows dict."""
    fake_rows = [{"_row_index": 2, "Email ID": "a@b.com", "SPOC": "Alice"}]
    cfg = _fake_cfg()

    with (
        patch("app.email_automation.outreach.sync.asyncio.to_thread") as mock_thread,
        patch("app.email_automation.outreach.sync.OutreachSheetClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.list_tab_names.return_value = ["test_campaign_2026_05"]
        mock_client.read_prospect_rows.return_value = fake_rows
        mock_client_cls.return_value = mock_client

        # asyncio.to_thread(fn) → call fn() in tests
        mock_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)

        result = await ingest_campaign_sheet(cfg)

    assert result["campaign"] == "test_campaign"
    assert result["rows"] == fake_rows
    assert result["error"] is None


@pytest.mark.asyncio
async def test_ingest_campaign_sheet_missing_tab_returns_empty():
    """When the monthly tab doesn't exist yet, returns empty rows (not an error)."""
    cfg = _fake_cfg()

    with (
        patch("app.email_automation.outreach.sync.asyncio.to_thread") as mock_thread,
        patch("app.email_automation.outreach.sync.OutreachSheetClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.list_tab_names.return_value = []  # tab absent
        mock_client_cls.return_value = mock_client
        mock_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)

        result = await ingest_campaign_sheet(cfg)

    assert result["rows"] == []
    assert result["error"] is None


@pytest.mark.asyncio
async def test_reconcile_campaign_db_inserts_new_lead():
    """reconcile_campaign_db persists a new lead from ingest data."""
    cfg = _fake_cfg()
    ingest_data = {
        "campaign": "test_campaign",
        "tab_name": "test_campaign_2026_05",
        "rows": [
            {
                "_row_index": 2,
                "Email ID": "new@lead.com",
                "SPOC": "Bob",
                "Account name": "Acme",
                "Designation": "CEO",
                "Industry": "Tech",
                "Company size - employee count": "50",
                "BD lead email ID": "bd@1mg.com",
                "BD head email ID": "",
                "Target service 1": "CHW",
                "Target service 2": "",
                "Status": "",
                "Source": "LinkedIn",
                "Sr. No.": "1",
                "Name of BD lead": "Dave",
            }
        ],
        "error": None,
    }
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    result = await reconcile_campaign_db(mock_db, cfg, ingest_data)

    assert result["inserted"] == 1
    assert result["updated"] == 0
