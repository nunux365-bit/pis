"""CampaignConfig — runtime-validated campaign definition."""
from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

_SLUG_RE = re.compile(r"^[a-z0-9_]+$")


class CampaignConfig(BaseModel):
    campaign_name: str
    gsheet_id: str
    sender_email: str
    body_template_path: str
    category_prompt_path: str
    subject: str
    tab_name: str | None = None  # override sheet tab; None = auto (campaign_name_YYYY_MM)
    attachment_path: str | None = None
    cc_emails: list[str] = []
    dashboard_display_name: str
    visible_columns: list[str]

    @field_validator("campaign_name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                f"campaign_name must be a lowercase slug (a-z, 0-9, _); got {v!r}"
            )
        return v

