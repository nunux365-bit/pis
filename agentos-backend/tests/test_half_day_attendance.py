from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.agents.o2c_ohc.mis_drafts import _attendance_records_for_period
from app.agents.o2c_ohc.ohc_roll_workbook_parser import _daywise_attendance_counts


def test_daywise_counts_half_day_variants_split_present_absent() -> None:
    counts = _daywise_attendance_counts(
        {
            "2026-02-01": "Half Day",
            "2026-02-02": "half-day",
            "2026-02-03": "HALFDAY",
            "2026-02-04": "Present ( G )",
        }
    )
    assert counts["present_days"] == Decimal("2.5")
    assert counts["absent_days"] == Decimal("1.5")


def test_attendance_records_for_period_half_day_variants_split_present_absent() -> None:
    rows = [
        {
            "employee_external_id": "E1",
            "ohc_attendance_daywise": {
                "2026-02-01": "Half Day",
                "2026-02-02": "half-day",
                "2026-02-03": "Present",
                "2026-02-04": "Absent",
            },
        }
    ]
    out = _attendance_records_for_period(
        rows,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 4),
    )
    assert out[0]["present_days"] == Decimal("2")
    assert out[0]["absent_days"] == Decimal("2")


def test_holiday_counts_in_week_off_days() -> None:
    counts = _daywise_attendance_counts(
        {
            "2026-02-01": "Holiday",
            "2026-02-02": "week off",
            "2026-02-03": "Present",
        }
    )
    assert counts["week_off_days"] == Decimal("2")
    assert counts["present_days"] == Decimal("1")

    rows = [
        {
            "employee_external_id": "E1",
            "ohc_attendance_daywise": {
                "2026-02-01": "Holiday",
                "2026-02-02": "Present",
            },
        }
    ]
    out = _attendance_records_for_period(
        rows,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 2),
    )
    assert out[0]["week_off_days"] == 1.0
    assert out[0]["present_days"] == Decimal("1")
    assert out[0]["expected_days"] == Decimal("1")


def test_left_does_not_count_as_present_daywise() -> None:
    counts = _daywise_attendance_counts(
        {
            "2026-02-01": "Left",
            "2026-02-02": "left",
            "2026-02-03": "-",
            "2026-02-04": "Present",
            "2026-02-05": "Locum Duty ( G ) / X",
        }
    )
    assert counts["present_days"] == Decimal("3")
    assert counts["blank_days"] == Decimal("0")


def test_left_does_not_count_as_present_period_scope() -> None:
    rows = [
        {
            "employee_external_id": "E1",
            "ohc_attendance_daywise": {
                "2026-02-01": "Left",
                "2026-02-02": "-",
                "2026-02-03": "Present",
            },
        }
    ]
    out = _attendance_records_for_period(
        rows,
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 3),
    )
    assert out[0]["present_days"] == Decimal("2")
    assert out[0]["total_days"] == 3
