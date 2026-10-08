"""Shared date helpers for O2C services (no FastAPI dependency)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def coerce_sql_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def prev_month_range(today: date) -> tuple[date, date]:
    this_month_start = today.replace(day=1)
    prev_month_end = this_month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    return prev_month_start, prev_month_end
