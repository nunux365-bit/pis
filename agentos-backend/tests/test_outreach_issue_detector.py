"""Unit tests for outreach issue detector — no DB or network calls."""
from __future__ import annotations
import pytest
from app.email_automation.outreach.issue_detector import detect_issues, IssueCode


def _row(**overrides):
    base = {
        "email_id": "test@example.com",
        "primary_email": "test@example.com",
        "spoc": "Shamreen",
        "bd_lead_email": "lead@1mg.com",
        "bd_head_email": "head@1mg.com",
        "col_m_raw": "",         # Col M value from sheet — empty = no manual override
        "is_sent": False,
        "lead_data_changed": False,
        "is_removed_from_sheet": False,
        "is_duplicate_email": False,
        "send_attempt_count": 0,
        "max_attempts": 5,
    }
    base.update(overrides)
    return base


def test_no_issues_clean_row():
    assert detect_issues(_row()) == []


def test_missing_email():
    issues = detect_issues(_row(email_id="", primary_email=""))
    assert IssueCode.MISSING_EMAIL in issues


def test_invalid_email():
    issues = detect_issues(_row(email_id="not-an-email", primary_email="not-an-email"))
    assert IssueCode.INVALID_EMAIL in issues


def test_valid_email_no_issue():
    issues = detect_issues(_row(email_id="user@domain.co.in", primary_email="user@domain.co.in"))
    assert IssueCode.INVALID_EMAIL not in issues
    assert IssueCode.MISSING_EMAIL not in issues


def test_missing_spoc():
    issues = detect_issues(_row(spoc=""))
    assert IssueCode.MISSING_SPOC in issues


def test_duplicate_email_flag():
    issues = detect_issues(_row(is_duplicate_email=True))
    assert IssueCode.DUPLICATE_EMAIL in issues


def test_manual_status_override():
    issues = detect_issues(_row(col_m_raw="Done"))
    assert IssueCode.MANUAL_STATUS_OVERRIDE in issues


def test_manual_status_override_system_values_ignored():
    # "sent" and "not sent" are system-written values — not manual overrides
    assert IssueCode.MANUAL_STATUS_OVERRIDE not in detect_issues(_row(col_m_raw="sent"))
    assert IssueCode.MANUAL_STATUS_OVERRIDE not in detect_issues(_row(col_m_raw="not sent"))
    assert IssueCode.MANUAL_STATUS_OVERRIDE not in detect_issues(_row(col_m_raw=""))


def test_data_modified_post_send():
    issues = detect_issues(_row(is_sent=True, lead_data_changed=True))
    assert IssueCode.DATA_MODIFIED_POST_SEND in issues


def test_data_not_modified_post_send_no_issue():
    issues = detect_issues(_row(is_sent=True, lead_data_changed=False))
    assert IssueCode.DATA_MODIFIED_POST_SEND not in issues


def test_removed_from_sheet():
    issues = detect_issues(_row(is_removed_from_sheet=True))
    assert IssueCode.REMOVED_FROM_SHEET in issues


def test_send_failed():
    issues = detect_issues(_row(send_attempt_count=5, max_attempts=5))
    assert IssueCode.SEND_FAILED in issues


def test_blocking_issues_are_subset():
    from app.email_automation.outreach.issue_detector import BLOCKING_ISSUES
    assert IssueCode.MISSING_EMAIL in BLOCKING_ISSUES
    assert IssueCode.INVALID_EMAIL in BLOCKING_ISSUES
    assert IssueCode.MISSING_SPOC in BLOCKING_ISSUES
    assert IssueCode.DUPLICATE_EMAIL in BLOCKING_ISSUES
    # Informational — must NOT be blocking
    assert IssueCode.DATA_MODIFIED_POST_SEND not in BLOCKING_ISSUES
    assert IssueCode.MANUAL_STATUS_OVERRIDE not in BLOCKING_ISSUES
    assert IssueCode.REMOVED_FROM_SHEET not in BLOCKING_ISSUES
    assert IssueCode.SEND_FAILED not in BLOCKING_ISSUES
