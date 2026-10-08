"""
Merge duplicate Excel columns that share the same calendar day (wide roll / shift layouts).

**Leftmost cell wins**; if the first slot is blank, the day stays blank (later columns are ignored).
Shared by ``ohc_roll_workbook_parser`` and ``roll_to_summary_export``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def normalize_day_cell_attendance_value(v: Any) -> str | None:
    """
    Attendance day cells must be **text** statuses. Pure numbers, dates, booleans, or empty
    are treated as **blank** (unmarked) for counting.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return None
    if isinstance(v, (datetime, date)):
        return None
    s = str(v).strip()
    return s if s else None


def collapse_duplicate_day_cells(raw_values: list[Any]) -> str | None:
    """
    Merge 2+ cells for the same calendar day (typical G/M shift pairs or duplicate date headers).

    Uses the **first cell left-to-right**, preserving blanks. This prevents blank/empty cells from
    being converted into present/absent when a duplicate day column has a non-empty value.

    Examples:
    - ``["", "Present ( G )"]`` -> ``None`` (blank preserved)
    - ``["Week Off", "Present ( G )"]`` -> ``"Week Off"``
    - ``["Present ( G )", "Week Off"]`` -> ``"Present ( G )"``
    """
    if not raw_values:
        return None
    first = raw_values[0]
    t = normalize_day_cell_attendance_value(first)
    if t:
        return t
    return None
