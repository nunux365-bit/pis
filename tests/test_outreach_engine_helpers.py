"""Tests for extracted engine helpers."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.email_automation.outreach.config import CampaignConfig
from app.email_automation.outreach.engine import dispatch_campaign, writeback_dispatch_status


def _fake_cfg() -> CampaignConfig:
    return CampaignConfig(
        campaign_name="test",
        gsheet_id="s1",
        tab_name="test_campaign_2026_05",
        sender_email="s@e.com",
        subject="Test outreach subject",
        body_template_path="chw_outreach.html",
        category_prompt_path="prompts/chw_outreach_reply.txt",
        attachment_path=None,
        dashboard_display_name="Test",
        visible_columns=["SPOC", "Status"],
    )


@pytest.mark.asyncio
async def test_dispatch_campaign_returns_writeback_updates():
    """dispatch_campaign now returns (stats, writeback_updates) tuple."""
    cfg = _fake_cfg()
    mock_db = AsyncMock()
    # No pending leads → empty writeback
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    stats, writeback_updates = await dispatch_campaign(mock_db, cfg)

    assert stats == {"sent": 0, "failed": 0, "skipped": 0}
    assert writeback_updates == []


@pytest.mark.asyncio
async def test_writeback_dispatch_status_calls_sheet_client():
    """writeback_dispatch_status calls OutreachSheetClient.write_back_status."""
    cfg = _fake_cfg()
    writeback_updates = [("tab_a", 2, "sent", "2026-01-01T00:00:00+0000")]

    with patch("app.email_automation.outreach.engine.OutreachSheetClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        with patch("app.email_automation.outreach.engine.asyncio.to_thread",
                   side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
            await writeback_dispatch_status(cfg, writeback_updates)

    mock_client.write_back_status.assert_called_once()
