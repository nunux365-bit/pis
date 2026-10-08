"""Merge contract_terms_version effective date spans (hygiene / MIS approve)."""

from __future__ import annotations

from datetime import date

_FAR_FUTURE = date(9999, 12, 31)


def merged_effective_dates(
    *,
    current_from: date | None,
    current_to: date | None,
    period_start: date | None,
    period_end: date | None,
    extra_froms: list[date] | None = None,
    extra_to_caps: list[date] | None = None,
) -> tuple[date, date | None]:
    """Return (effective_from, effective_to) with open end as None."""
    froms: list[date] = []
    if current_from:
        froms.append(current_from)
    if period_start:
        froms.append(period_start)
    if extra_froms:
        froms.extend(extra_froms)
    to_caps: list[date] = []
    if current_to:
        to_caps.append(current_to)
    if period_end:
        to_caps.append(period_end)
    if extra_to_caps:
        to_caps.extend(extra_to_caps)
    new_from = min(froms) if froms else (period_start or date.today())
    max_cap = max(to_caps) if to_caps else _FAR_FUTURE
    new_to = None if max_cap >= _FAR_FUTURE else max_cap
    return new_from, new_to
