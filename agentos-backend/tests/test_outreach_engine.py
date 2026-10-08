"""Tests for OutreachEngine dispatch logic."""
from __future__ import annotations
from pathlib import Path
import pytest
from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent.parent / "app/email_automation/outreach/templates"


def test_chw_template_renders_spoc_name():
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))
    tmpl = env.get_template("chw/body.html.j2")
    html = tmpl.render(spoc_name="Shamreen")
    assert "Dear Shamreen," in html
    assert "TATA 1mg" in html
    assert "capabilities deck" in html


def test_chw_template_no_undefined_variables():
    """Template must not contain undefined Jinja2 variables beyond spoc_name."""
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)))
    tmpl = env.get_template("chw/body.html.j2")
    html = tmpl.render(spoc_name="Test")
    assert "Undefined" not in html


from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from app.email_automation.outreach.engine import render_email, _parse_recipients


def test_parse_recipients_to():
    to, cc, bcc = _parse_recipients(
        email_id="a@x.com & b@x.com",
        bd_lead_email="lead@1mg.com",
        bd_head_email="head@1mg.com",
    )
    assert "a@x.com" in to
    assert "b@x.com" in to
    assert "lead@1mg.com" in cc
    assert "head@1mg.com" in bcc


def test_parse_recipients_multiple_cc():
    _, cc, _ = _parse_recipients(
        email_id="prospect@co.com",
        bd_lead_email="a@1mg.com; b@1mg.com",
        bd_head_email="head@1mg.com",
    )
    assert "a@1mg.com" in cc
    assert "b@1mg.com" in cc


def test_render_email_subject():
    from app.email_automation.outreach.config import CampaignConfig
    cfg = CampaignConfig(
        campaign_name="chw_cold_outreach",
        gsheet_id="fake",
        tab_name="Sheet1",
        sender_email="corporatehealth@1mg.com",
        subject="Introducing Tata 1mg's Corporate Health and Wellness Services",
        body_template_path="chw/body.html.j2",
        category_prompt_path="chw/categorizer_prompt.txt",
        attachment_path=None,
        dashboard_display_name="CHW",
        visible_columns=[],
    )
    subject, html = render_email(cfg=cfg, spoc_name="Alice")
    assert subject == cfg.subject
    assert "Tata 1mg" in subject
    assert "Dear Alice," in html
