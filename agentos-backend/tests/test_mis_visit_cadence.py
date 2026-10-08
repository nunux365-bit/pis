"""Weekly visit cadence: 4-week vs 5-week months (create-new per_visit)."""

from __future__ import annotations

import inspect
import json
import math
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.o2c_ohc.mis_db import (
    _visits_from_schedule_config,
    create_contract_rate_line_and_mis_summary_row,
)
from app.agents.o2c_ohc.mis_summary_llm import _mis_summary_openai_chat_inputs
from app.agents.o2c_ohc.o2c_utils import _billing_weeks_inclusive, _calendar_days_inclusive
from app.api.routes.o2c import CreateNewMisSummaryRowBody


def test_billing_weeks_inclusive_4_vs_5() -> None:
    assert _billing_weeks_inclusive(date(2026, 2, 1), date(2026, 2, 28)) == 4
    assert _billing_weeks_inclusive(date(2026, 3, 1), date(2026, 3, 31)) == 5
    assert _billing_weeks_inclusive(date(2026, 4, 1), date(2026, 4, 30)) == 5
    assert _billing_weeks_inclusive(date(2024, 2, 1), date(2024, 2, 29)) == 5


def test_visits_from_weekly_cadence_uses_period_weeks() -> None:
    sch = {"_type": "frequency", "visits_per_week": 2, "days_flexible": True}
    feb = _visits_from_schedule_config(
        sch, period_start=date(2026, 2, 1), period_end=date(2026, 2, 28)
    )
    mar = _visits_from_schedule_config(
        sch, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
    )
    assert feb == Decimal("8.00")
    assert mar == Decimal("10.00")


def test_visits_from_frozen_month_unchanged() -> None:
    sch = {"visits_per_month": 8}
    assert _visits_from_schedule_config(
        sch, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
    ) == Decimal(8)


def test_weekly_wins_over_frozen_month() -> None:
    sch = {"visits_per_week": 2, "visits_per_month": 8}
    mar = _visits_from_schedule_config(
        sch, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
    )
    assert mar == Decimal("10.00")


def test_weekly_without_period_falls_back_to_month() -> None:
    sch = {"visits_per_week": 2, "visits_per_month": 8}
    assert _visits_from_schedule_config(sch) == Decimal(8)


def test_llm_payload_does_not_inject_visits_this_period() -> None:
    src = inspect.getsource(_mis_summary_openai_chat_inputs)
    assert "visits_this_period" not in src
    assert "_visit_cadence_for_llm_period" not in src


def test_create_new_stores_week_and_month_visit_fields() -> None:
    src = inspect.getsource(create_contract_rate_line_and_mis_summary_row)
    assert 'sch_obj["visits_per_week"]' in src
    assert 'sch_obj["visits_per_month"]' in src
    assert "not both" in src
    assert "visits_per_week (or visit_per_month)" in src
    assert "_billing_weeks_inclusive" in inspect.getsource(_visits_from_schedule_config)


def test_billing_weeks_matches_js_ceil_days_over_7() -> None:
    """UI ``Math.ceil(days / 7)`` must match Python ``(days + 6) // 7``."""
    samples = [
        date(2026, 2, 1),
        date(2026, 2, 28),
        date(2026, 3, 1),
        date(2026, 3, 31),
        date(2026, 5, 1),
        date(2026, 5, 31),
        date(2024, 2, 1),
        date(2024, 2, 29),
    ]
    for d0 in samples:
        for d1 in samples:
            if d1 < d0:
                continue
            py = _billing_weeks_inclusive(d0, d1)
            days = _calendar_days_inclusive(d0, d1)
            js = max(1, math.ceil(days / 7))
            assert py == js, (d0, d1, py, js)


@pytest.mark.parametrize(
    ("start", "end", "weeks"),
    [
        (date(2026, 5, 1), date(2026, 5, 1), 1),
        (date(2026, 5, 1), date(2026, 5, 7), 1),
        (date(2026, 5, 1), date(2026, 5, 8), 2),
        (date(2026, 5, 31), date(2026, 5, 1), 1),
    ],
)
def test_billing_weeks_short_and_inverted_periods(
    start: date, end: date, weeks: int
) -> None:
    assert _billing_weeks_inclusive(start, end) == weeks


def test_visits_from_json_string_and_zero_week_falls_back() -> None:
    sch = json.dumps({"visits_per_week": 2, "visits_per_month": 8})
    assert _visits_from_schedule_config(
        sch, period_start=date(2026, 3, 1), period_end=date(2026, 3, 31)
    ) == Decimal("10.00")
    assert _visits_from_schedule_config(
        {"visits_per_week": 0, "visits_per_month": 8},
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    ) == Decimal(8)
    assert _visits_from_schedule_config({}) is None
    assert _visits_from_schedule_config("not-json") is None


def _create_new_body(**extra: object) -> CreateNewMisSummaryRowBody:
    base: dict[str, object] = {
        "mis_run_id": uuid4(),
        "description": "Doctor",
        "role_code": "FMO_MBBS_AFIH",
        "rate_amount": Decimal("19286"),
        "billing_model": "per_visit",
    }
    base.update(extra)
    return CreateNewMisSummaryRowBody.model_validate(base)


def test_create_new_api_accepts_week_or_month_not_both() -> None:
    week = _create_new_body(visits_per_week=Decimal("1"))
    assert week.visits_per_week == Decimal("1")
    assert week.visit_per_month is None
    month = _create_new_body(visit_per_month=Decimal("4"))
    assert month.visit_per_month == Decimal("4")
    with pytest.raises(ValidationError, match="not both"):
        _create_new_body(visits_per_week=Decimal("1"), visit_per_month=Decimal("4"))
