"""Order RCA shared time helpers."""

from __future__ import annotations

from app.agents.order_rca.time_utils import format_duration_minutes


def test_format_duration_minutes_up_to_one_hour():
    assert format_duration_minutes(45) == "45 min"
    assert format_duration_minutes(60) == "60 min"


def test_format_duration_minutes_hours():
    assert format_duration_minutes(61) == "1 h 1 min"
    assert format_duration_minutes(390) == "6 h 30 min"
    assert format_duration_minutes(120) == "2 h"


def test_format_duration_minutes_days():
    assert format_duration_minutes(3506) == "2 d 10 h 26 min"
    assert format_duration_minutes(24 * 60) == "24 h"
    assert format_duration_minutes(24 * 60 + 1) == "1 d 1 min"
