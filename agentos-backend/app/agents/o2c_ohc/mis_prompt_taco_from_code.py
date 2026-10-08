"""
TACO MIS user prompt — **deterministic MIS billing engine** (single file).

Body: ``_mis_taco_worldclass_from_code_prompt.txt`` — global hard rules, STEPs 1–7 (model,
``per_shift_missed`` → STEP 3C / ``C_SHIFT_MISS``, time vs frequency, SC, headcount, admin), FAILSAFE,
``{schema_json}``, Context + JSON.

Placeholders for ``str.format`` match ``build_mis_summary_user_prompt`` /
``mis_summary_llm._mis_summary_openai_chat_inputs``.
"""

from __future__ import annotations

from pathlib import Path

MIS_PROMPT_TACO_POLICY_VERSION = "taco-ohc-deterministic-engine-v13"

_PROMPT_PATH = Path(__file__).with_name("_mis_taco_worldclass_from_code_prompt.txt")


def mis_taco_worldclass_user_prompt_unformatted() -> str:
    """Unexpanded TACO user message (``{schema_json}``, ``{period_start}``, … placeholders)."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_mis_taco_worldclass_user_prompt(
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
    return mis_taco_worldclass_user_prompt_unformatted().format(
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
    "MIS_PROMPT_TACO_POLICY_VERSION",
    "build_mis_taco_worldclass_user_prompt",
    "mis_taco_worldclass_user_prompt_unformatted",
]
