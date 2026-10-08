"""Shared helpers for O2C OHC (MIS / attendance paths; no invoice batching)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


def _working_days_inclusive(start: date, end: date) -> int:
    if end < start:
        return 1
    n = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return max(n, 1)


def _calendar_days_inclusive(start: date, end: date) -> int:
    """Inclusive calendar span of the billing period (used in MIS prompts / second-pass checks)."""
    if end < start:
        return 1
    return (end - start).days + 1


def _billing_weeks_inclusive(start: date, end: date) -> int:
    """Weeks in an inclusive billing period: ceil(calendar days / 7).

    A 28-day February is 4 weeks; 29–31 day months are 5. Used for
    ``visits_per_week`` cadence (not a frozen visits-per-month).
    """
    days = _calendar_days_inclusive(start, end)
    return max(1, (days + 6) // 7)


@dataclass(frozen=True)
class O2cAttendanceSiteSkip:
    """Attendance site key did not produce an MIS draft (recon / dashboard queue)."""

    client_site_key: str
    reason_code: str
    detail: str = ""
    attendance_row_count: int = 0
    llm_match_attempted: bool = False
