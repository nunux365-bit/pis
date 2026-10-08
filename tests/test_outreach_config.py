"""Tests for CampaignConfig validation."""
from __future__ import annotations
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.email_automation.outreach.config import CampaignConfig


def _base_kwargs(**overrides):
    return {
        "campaign_name": "chw_cold_outreach",
        "gsheet_id": "1ZlAKVedt85Vzj8XAUlKRQ8HoNWtJYyK-v6Ah_45HJu0",
        "tab_name": "Sheet1",
        "sender_email": "corporatehealth@1mg.com",
        "subject": "Introducing Tata 1mg's Corporate Health and Wellness Services",
        "body_template_path": "chw/body.html.j2",
        "category_prompt_path": "chw/categorizer_prompt.txt",
        "attachment_path": None,
        "dashboard_display_name": "CHW Cold Outreach",
        "visible_columns": ["account_name", "spoc", "status"],
        **overrides,
    }


def test_valid_config_builds():
    cfg = CampaignConfig(**_base_kwargs())
    assert cfg.campaign_name == "chw_cold_outreach"
    assert cfg.attachment_path is None


def test_campaign_name_no_spaces():
    with pytest.raises(ValidationError, match="campaign_name"):
        CampaignConfig(**_base_kwargs(campaign_name="chw cold outreach"))


def test_campaign_name_slug_valid():
    cfg = CampaignConfig(**_base_kwargs(campaign_name="chw_cold_outreach_2026"))
    assert cfg.campaign_name == "chw_cold_outreach_2026"
