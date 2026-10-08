"""contract_terms_dates.merge_effective_dates"""

from __future__ import annotations

from datetime import date

from app.agents.o2c_ohc.contract_terms_dates import merged_effective_dates


def test_merged_effective_dates_open_end() -> None:
    ef, et = merged_effective_dates(
        current_from=date(2025, 10, 1),
        current_to=None,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        extra_froms=[date(2025, 9, 1)],
        extra_to_caps=[date(2028, 9, 30)],
    )
    assert ef == date(2025, 9, 1)
    assert et == date(2028, 9, 30)


def test_merged_effective_dates_from_period_when_no_current() -> None:
    ef, et = merged_effective_dates(
        current_from=None,
        current_to=None,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    assert ef == date(2026, 4, 1)
    assert et == date(2026, 4, 30)
