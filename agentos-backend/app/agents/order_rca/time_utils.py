"""Shared time parsing and IST display for Order RCA (no rules/eta_resolution coupling)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.order_rca.constants import ORDER_RCA_DISPLAY_TZ_LABEL, ORDER_RCA_DISPLAY_TZ_NAME

ORDER_RCA_API_TZ = UTC
ORDER_RCA_DISPLAY_TZ = ZoneInfo(ORDER_RCA_DISPLAY_TZ_NAME)


def parse_num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        t = value.strip()
        return t or None
    return str(value)


def parse_unix_ts(value: Any) -> datetime | None:
    """Unix epoch → true UTC instant."""
    n = parse_num(value)
    if n is None or n <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(n), tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def parse_eta_to_unix(value: Any) -> datetime | None:
    """``order.eta.eta_to`` unix — IST wall clock stored in the UTC unix slot."""
    n = parse_num(value)
    if n is None or n <= 0:
        return None
    try:
        utc_wall = datetime.fromtimestamp(int(n), tz=UTC)
        return datetime(
            utc_wall.year,
            utc_wall.month,
            utc_wall.day,
            utc_wall.hour,
            utc_wall.minute,
            utc_wall.second,
            utc_wall.microsecond,
            tzinfo=ORDER_RCA_DISPLAY_TZ,
        )
    except (OSError, OverflowError, ValueError):
        return None


def anchor_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ORDER_RCA_DISPLAY_TZ)
    return dt.astimezone(ORDER_RCA_DISPLAY_TZ)


def format_date_ist(dt: datetime) -> str:
    local = dt.astimezone(ORDER_RCA_DISPLAY_TZ)
    return local.strftime(f"%d %b, %Y {ORDER_RCA_DISPLAY_TZ_LABEL}")


def format_instant_ist(dt: datetime, *, date_only: bool = False) -> str:
    if date_only:
        return format_date_ist(dt)
    local = dt.astimezone(ORDER_RCA_DISPLAY_TZ)
    return local.strftime(f"%d %b, %Y %H:%M {ORDER_RCA_DISPLAY_TZ_LABEL}")


def format_ist_wall(dt: datetime, *, date_only: bool = False) -> str:
    """Format a datetime already anchored in Asia/Kolkata."""
    dt = anchor_ist(dt)
    if date_only:
        return dt.strftime(f"%d %b, %Y {ORDER_RCA_DISPLAY_TZ_LABEL}")
    return dt.strftime(f"%d %b, %Y %H:%M {ORDER_RCA_DISPLAY_TZ_LABEL}")


def format_duration_minutes(minutes: int | float | None, *, suffix: str = "") -> str | None:
    """
    Human-readable elapsed time for RCA display and LLM narrative.

    - ≤60 min: ``N min``
    - >60 min, ≤24 h: ``H h M min``
    - >24 h: ``D d H h M min`` (omit zero trailing parts)
    """
    if minutes is None:
        return None
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    if total <= 60:
        return f"{total} min{suffix}"
    if total <= 24 * 60:
        hours, mins = divmod(total, 60)
        if mins == 0:
            return f"{hours} h{suffix}"
        return f"{hours} h {mins} min{suffix}"
    days, rem = divmod(total, 24 * 60)
    hours, mins = divmod(rem, 60)
    parts: list[str] = [f"{days} d"]
    if hours:
        parts.append(f"{hours} h")
    if mins:
        parts.append(f"{mins} min")
    return " ".join(parts) + suffix
