"""Tests for reply_scanner and reply_classifier."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.email_automation.outreach.reply_scanner import scan_unclassified_threads
from app.email_automation.outreach.reply_classifier import classify_threads_batch


@pytest.mark.asyncio
async def test_scan_unclassified_threads_returns_candidates():
    """scan_unclassified_threads returns rows for sent leads with unclassified threads."""
    mock_db = AsyncMock()
    mock_row = MagicMock()
    mock_row.lead_id = 42
    mock_row.gmail_thread_id = "thread_abc"
    mock_row.campaign_name = "test_campaign"
    mock_row.latest_reply_message_id = None

    mock_result = MagicMock()
    mock_result.all.return_value = [mock_row]
    mock_db.execute.return_value = mock_result

    candidates = await scan_unclassified_threads(mock_db)

    assert len(candidates) == 1
    assert candidates[0]["lead_id"] == 42
    assert candidates[0]["thread_id"] == "thread_abc"
    assert candidates[0]["campaign_name"] == "test_campaign"


@pytest.mark.asyncio
async def test_scan_unclassified_threads_empty():
    """scan_unclassified_threads returns empty when no candidates."""
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_db.execute.return_value = mock_result

    candidates = await scan_unclassified_threads(mock_db)

    assert candidates == []


@pytest.mark.asyncio
async def test_classify_threads_batch_returns_results():
    """classify_threads_batch returns classification dicts per thread."""
    from app.email_automation.outreach.config import CampaignConfig
    candidates = [{"lead_id": 1, "thread_id": "t1", "campaign_name": "camp_a", "latest_reply_message_id": None}]
    campaigns = [
        CampaignConfig(
            campaign_name="camp_a",
            gsheet_id="s1",
            gsheet_reader_email="r@e.com",
            sender_email="s@e.com",
            subject="Test outreach subject",
            body_template_path="chw_outreach.html",
            category_prompt_path="prompts/chw_outreach_reply.txt",
            attachment_path=None,
            dashboard_display_name="Camp A",
            visible_columns=["SPOC", "Status"],
        )
    ]

    fake_payload = {
        "category": "interested",
        "confidence": 0.95,
        "justification": "Expressed interest",
        "prompt_version": "chw_outreach_reply_v1",
    }

    with (
        patch("app.email_automation.outreach.reply_classifier.gmail_sa") as mock_gmail,
        patch(
            "app.email_automation.outreach.reply_classifier.classify_outreach_thread_transcript",
            new=AsyncMock(return_value=fake_payload),
        ),
        patch(
            "app.email_automation.outreach.reply_classifier.asyncio.to_thread",
            side_effect=lambda fn, *a, **kw: fn(*a, **kw),
        ),
    ):
        mock_gmail.fetch_thread_full.return_value = {
            "messages": [
                {
                    "id": "msg1",
                    "internalDate": "1000000",
                    "payload": {
                        "headers": [{"name": "From", "value": "client@acme.com"}]
                    },
                }
            ]
        }
        mock_gmail.message_resource_internal_date_ms.return_value = 1000000
        mock_gmail.message_resource_plain_text.return_value = "I am interested."

        results = await classify_threads_batch(candidates, campaigns)

    assert len(results) == 1
    assert results[0]["lead_id"] == 1
    assert results[0]["category"] == "interested"
    assert results[0]["gmail_message_id"] == "msg1"
    assert results[0]["skipped"] is False
