"""Stateless issue detection for outreach leads."""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SPLIT_RE = re.compile(r"[;&]")
_SYSTEM_COL_M_VALUES = {"sent", "not sent", ""}


def _has_any_address(raw: str | None) -> bool:
    """Return True if raw contains at least one non-empty token after splitting on ; or &."""
    if not raw:
        return False
    return any(tok.strip() for tok in _SPLIT_RE.split(raw))


class IssueCode(StrEnum):
    MISSING_EMAIL = "missing_email"
    INVALID_EMAIL = "invalid_email"
    MISSING_SPOC = "missing_spoc"
    DUPLICATE_EMAIL = "duplicate_email"
    MANUAL_STATUS_OVERRIDE = "manual_status_override"
    DATA_MODIFIED_POST_SEND = "data_modified_post_send"
    REMOVED_FROM_SHEET = "removed_from_sheet"
    SEND_FAILED = "send_failed"
    # Non-blocking — email sends to primary recipient; CC/BCC will be absent
    MISSING_BD_LEAD_EMAIL = "missing_bd_lead_email"
    MISSING_BD_HEAD_EMAIL = "missing_bd_head_email"


BLOCKING_ISSUES: frozenset[IssueCode] = frozenset({
    IssueCode.MISSING_EMAIL,
    IssueCode.INVALID_EMAIL,
    IssueCode.MISSING_SPOC,
    IssueCode.DUPLICATE_EMAIL,
    IssueCode.MISSING_BD_LEAD_EMAIL,
    IssueCode.MISSING_BD_HEAD_EMAIL,
})


def detect_issues(row: dict[str, Any]) -> list[IssueCode]:
    """Return active issue codes for one lead.

    ``row`` keys:
        primary_email, email_id, spoc, col_m_raw,
        is_sent, lead_data_changed, is_removed_from_sheet,
        is_duplicate_email, send_attempt_count, max_attempts,
        bd_lead_email (optional), bd_head_email (optional)
    """
    issues: list[IssueCode] = []
    email = (row.get("primary_email") or "").strip()

    if not email:
        issues.append(IssueCode.MISSING_EMAIL)
    elif not _EMAIL_RE.match(email):
        issues.append(IssueCode.INVALID_EMAIL)

    if not (row.get("spoc") or "").strip():
        issues.append(IssueCode.MISSING_SPOC)

    if not _has_any_address(row.get("bd_lead_email")):
        issues.append(IssueCode.MISSING_BD_LEAD_EMAIL)

    if not _has_any_address(row.get("bd_head_email")):
        issues.append(IssueCode.MISSING_BD_HEAD_EMAIL)

    if row.get("is_duplicate_email"):
        issues.append(IssueCode.DUPLICATE_EMAIL)

    col_m = (row.get("col_m_raw") or "").strip().lower()
    if col_m not in _SYSTEM_COL_M_VALUES:
        issues.append(IssueCode.MANUAL_STATUS_OVERRIDE)

    if row.get("is_sent") and row.get("lead_data_changed"):
        issues.append(IssueCode.DATA_MODIFIED_POST_SEND)

    if row.get("is_removed_from_sheet"):
        issues.append(IssueCode.REMOVED_FROM_SHEET)

    if (row.get("send_attempt_count") or 0) >= (row.get("max_attempts") or 5):
        if row.get("send_attempt_count", 0) > 0:
            issues.append(IssueCode.SEND_FAILED)

    return issues
