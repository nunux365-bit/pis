"""Static campaign definitions — source of truth for all active outreach campaigns.

To add a campaign: append a new CampaignConfig entry to ACTIVE_CAMPAIGNS.
To change schedule timing: edit SCHEDULE below.
To change any other value (sheet ID, sender, template path): edit here and redeploy.

Schedule trigger dicts are passed directly to APScheduler's add_job().
Examples:
    {"trigger": "cron", "hour": 1, "minute": 30}          # daily at 01:30 UTC
    {"trigger": "cron", "day_of_week": "mon", "hour": 3}  # weekly Monday 03:00 UTC
    {"trigger": "interval", "hours": 6}                    # every 6 hours
    {"trigger": "interval", "minutes": 30}                 # every 30 minutes
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.settings import settings
from app.email_automation.outreach.config import CampaignConfig


@dataclass
class OutreachSchedule:
    sync: dict[str, Any]     # sheet sync trigger
    send: dict[str, Any]     # email dispatch trigger
    replies: dict[str, Any]  # reply scan + classify trigger


SCHEDULE = OutreachSchedule(
    sync={"trigger": "cron", "hour": 1, "minute": 30},             # daily 07:00 IST
    send={"trigger": "cron", "day_of_week": "thu", "hour": 3, "minute": 0},   # Thursday 08:30 IST
    replies={"trigger": "interval", "hours": 6},          # every 6 hours
)

ACTIVE_CAMPAIGNS: list[CampaignConfig] = [
    CampaignConfig(
        campaign_name="chw_outreach",
        tab_name="Sheet1",
        sender_email="corporatehealth@1mg.com",
        subject="Introducing Tata 1mg's Corporate Health and Wellness Services",
        gsheet_id=settings.chw_outreach_gsheet_id,
        body_template_path="chw/body.html.j2",
        category_prompt_path="chw/categorizer_prompt.txt",
        attachment_path="chw/Tata 1mg Corporate Health and Wellness Brochure.pdf",
        cc_emails=[],  # add campaign-level CC addresses here e.g. ["name@1mg.com"]
        dashboard_display_name="CHW Cold Outreach",
        visible_columns=["SPOC", "Account name", "Designation", "Email ID", "Status"],
    ),
]
