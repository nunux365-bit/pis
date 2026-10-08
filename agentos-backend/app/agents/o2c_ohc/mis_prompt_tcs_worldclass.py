"""
**TCS** MIS user prompt — **deterministic OHC engine** (production ``billing_profile=tcs``).

Template: ``_mis_tcs_worldclass_from_code_prompt.txt`` via ``mis_prompt_tcs_v30`` builders.
"""

from __future__ import annotations

from app.agents.o2c_ohc.mis_prompt_tcs_v30 import (
    MIS_PROMPT_TCS_POLICY_VERSION,
    build_mis_tcs_v30_user_prompt,
    mis_tcs_v30_user_prompt_unformatted,
)

MIS_TCS_WORLDCLASS_POLICY_VERSION = MIS_PROMPT_TCS_POLICY_VERSION


def mis_tcs_worldclass_user_prompt_unformatted() -> str:
    """Alias for the unexpanded TCS v3.0 template (exports / diff tools)."""
    return mis_tcs_v30_user_prompt_unformatted()


def build_mis_tcs_worldclass_user_prompt(
    *,
    client_site_key: str,
    period_start: str,
    period_end: str,
    working_days: int,
    calendar_days: int,
    schema_json: str,
    attendance_json: str,
    rate_lines_json: str,
) -> str:
    return build_mis_tcs_v30_user_prompt(
        client_site_key=client_site_key,
        period_start=period_start,
        period_end=period_end,
        working_days=int(working_days),
        calendar_days=int(calendar_days),
        schema_json=schema_json,
        attendance_json=attendance_json,
        rate_lines_json=rate_lines_json,
    )


__all__ = [
    "MIS_TCS_WORLDCLASS_POLICY_VERSION",
    "build_mis_tcs_worldclass_user_prompt",
    "mis_tcs_worldclass_user_prompt_unformatted",
]
