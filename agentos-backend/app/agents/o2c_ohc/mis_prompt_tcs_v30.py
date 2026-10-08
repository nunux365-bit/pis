"""
TCS MIS user prompt — **deterministic engine** (single file).

Body: ``_mis_tcs_worldclass_from_code_prompt.txt`` — procedural ROUTINE, TCS-PV
(``per_visit`` + monthly + cadence), MAP-STAFF-CAL / MAP waiver, 3B/3C, SC, cap, admin.

Placeholders for ``str.format`` match ``build_mis_summary_user_prompt`` /
``mis_summary_llm._mis_summary_openai_chat_inputs``.
"""

from __future__ import annotations

from pathlib import Path

MIS_PROMPT_TCS_POLICY_VERSION = "tcs-ohc-deterministic-engine-v10"

_PROMPT_PATH = Path(__file__).with_name("_mis_tcs_worldclass_from_code_prompt.txt")


def mis_tcs_v30_user_prompt_unformatted() -> str:
    """Unexpanded TCS user message (``{schema_json}``, ``{period_start}``, … placeholders)."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_mis_tcs_v30_user_prompt(
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
    return mis_tcs_v30_user_prompt_unformatted().format(
        schema_json=schema_json,
        client_site_key=client_site_key,
        period_start=period_start,
        period_end=period_end,
        working_days=int(working_days),
        calendar_days=int(calendar_days),
        attendance_json=attendance_json,
        rate_lines_json=rate_lines_json,
    )


__all__ = [
    "MIS_PROMPT_TCS_POLICY_VERSION",
    "build_mis_tcs_v30_user_prompt",
    "mis_tcs_v30_user_prompt_unformatted",
]
