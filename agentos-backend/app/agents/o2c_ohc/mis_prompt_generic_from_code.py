"""
Generic MIS user prompt — **worldclass FROM_CODE** bundle (single self-contained document).

Body: ``_mis_generic_worldclass_slim_prompt.txt`` — §0–§2.5 envelope (``{prompt_bundle_version}``),
§2.1 **``ROW_NET``** (full-precision row total) + **calc_notes_amount** (mirror only), §2.2 **anti-drift** (2dp round, **sum_B** scope, physician tie-break),
**generic** profile + Two-phase (**OHC_ADMIN_INVOICE_PCT**), then
the **same** worldclass MIS technical tail as sibling bundles (Execution mode through schema placeholders:
MATH-PRIMARY, DEFINITIONS, classifier, passes, gates). **Generic persistence:** server may quantize persisted
**``final_amount``** to two decimals. Bump ``MIS_PROMPT_GENERIC_POLICY_VERSION`` when that core or profile changes materially.

Placeholders for ``str.format`` match ``build_mis_summary_user_prompt`` /
``mis_summary_llm._mis_summary_openai_chat_inputs``.
"""

from __future__ import annotations

import functools
from pathlib import Path

MIS_PROMPT_GENERIC_POLICY_VERSION = "generic-worldclass-v2.83"

_PROMPT_PATH = Path(__file__).with_name("_mis_generic_worldclass_slim_prompt.txt")


def mis_generic_worldclass_user_prompt_unformatted() -> str:
    """Unexpanded generic user message (``{prompt_bundle_version}``, ``{schema_json}``, ``{period_start}``, …)."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def mis_generic_profile_block_for_prompt_profiles() -> str:
    """Profile-only section (for ``mis_profile_prompt_block`` / tests); must stay in sync with bundle file."""
    raw = mis_generic_worldclass_user_prompt_unformatted()
    start_key = "**Profile-specific rules (``billing_profile=generic``"
    end_key = "\n## Execution mode\n"
    i = raw.find(start_key)
    j = raw.find(end_key, i)
    if i == -1 or j == -1:
        raise RuntimeError("generic prompt bundle: profile block extraction markers missing")
    return raw[i:j].strip()


def build_mis_generic_worldclass_user_prompt(
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
    return mis_generic_worldclass_user_prompt_unformatted().format(
        prompt_bundle_version=MIS_PROMPT_GENERIC_POLICY_VERSION,
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
    "MIS_PROMPT_GENERIC_POLICY_VERSION",
    "build_mis_generic_worldclass_user_prompt",
    "mis_generic_profile_block_for_prompt_profiles",
    "mis_generic_worldclass_user_prompt_unformatted",
]
