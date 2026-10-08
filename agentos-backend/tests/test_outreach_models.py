"""Smoke-test that ORM models are importable and have expected columns."""
from app.db.models import OutreachLead, OutreachReply


def test_outreach_lead_columns():
    cols = {c.key for c in OutreachLead.__table__.columns}
    assert {"campaign_name", "primary_email", "status", "issues",
            "gsheet_row_index", "gmail_thread_id", "last_reply_category"} <= cols


def test_outreach_reply_columns():
    cols = {c.key for c in OutreachReply.__table__.columns}
    assert {"outreach_lead_id", "gmail_message_id", "category",
            "confidence", "prompt_version"} <= cols
